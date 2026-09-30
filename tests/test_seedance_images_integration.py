import asyncio
import os
from datetime import timedelta
from io import BytesIO
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import httpx
from openai import AsyncOpenAI
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.core.exceptions import AppException
from app.core.exceptions import register_exception_handlers
from app.api.deps import get_current_user
from app.api.v1.endpoints.uploads import router as uploads_router
from app.db.session import get_db
from app.core.timezone import beijing_datetime
from app.models.conversation import ConversationMessage
from app.models.seedance_image import SeedanceImage
from app.models.task_record import UserTaskRecord
from app.models.points import UserPointsTransaction
from app.schemas.conversation import ConversationSendMessageRequest
from app.schemas.upload import UploadFileOut
from app.services import seedance_images as service
from app.services.conversation import service as conversations
from app.services.generation import task_dispatch
from app.tasks import seedance_images as worker
from tests.test_text_conversation_integration import text_conversation_db  # noqa: F401


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_DB_INTEGRATION_TESTS") != "1",
        reason="requires isolated PostgreSQL",
    ),
]


@pytest.fixture
def review_setup(text_conversation_db, monkeypatch):  # noqa: F811
    data = text_conversation_db
    monkeypatch.setattr(settings, "apimart_api_key", "test-review-key")
    calls = []

    async def upload(file, **kwargs):
        calls.append(kwargs)
        return UploadFileOut(
            url=f"https://cdn.example/{uuid4()}.png",
            object_key="test/file.png",
            filename=file.filename,
            content_type="image/png",
            size=20,
            file_type="image",
        )

    monkeypatch.setattr(service, "upload_story_file", upload)
    monkeypatch.setattr(task_dispatch, "publish_task_message", lambda **kwargs: None)
    factory = async_sessionmaker(data.session.bind, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(worker, "WorkerSessionLocal", factory)
    return data, calls, factory


def file(name="one.png", content=b"original"):
    return SimpleNamespace(
        filename=name, content_type="image/png", file=BytesIO(b"\x89PNG\r\n\x1a\n" + content)
    )


async def due(db, image_id):
    record = await db.get(SeedanceImage, image_id)
    await db.refresh(record)
    record.next_poll_at = beijing_datetime() - timedelta(seconds=1)
    await db.commit()


@pytest.mark.asyncio
async def test_concurrent_uploads_reuse_file_and_review_record(review_setup):
    data, calls, factory = review_setup
    user_id = data.user.id

    async def upload(name):
        async with factory() as db:
            return await service.upload_seedance_image(db, user_id, file(name))

    a, b = await asyncio.gather(upload("one.png"), upload("renamed.png"))
    assert a.image_id == b.image_id
    assert sorted([a.reused, b.reused]) == [False, True]
    assert len(calls) == 1
    assert await data.session.scalar(select(func.count()).select_from(SeedanceImage)) == 1
    assert await data.session.scalar(select(func.count()).select_from(UserPointsTransaction)) == 0


@pytest.mark.asyncio
async def test_upload_review_reuse_and_generation_gate(review_setup, monkeypatch):
    data, calls, _ = review_setup
    db = data.session
    user_id, conversation_id = data.user.id, data.conversation.id
    data.model.vendor, data.model.model_id, data.model.model_type = (
        "apimart",
        "seedance-2.0",
        "video",
    )
    data.model.configuration = {"billing": {"base_points": 3}}
    model_id = data.model.id
    await db.commit()
    uploaded = await service.upload_seedance_image(db, user_id, file())
    request = ConversationSendMessageRequest(
        content="让@{图片1}动起来",
        ai_model_id=model_id,
        image_references=[{"name": "图片1", "image_id": uploaded.image_id}],
    )
    with pytest.raises(AppException):
        await conversations.send_conversation_message(db, conversation_id, data.user, request)
    await db.rollback()
    await db.refresh(data.user)
    for model in (ConversationMessage, UserTaskRecord, UserPointsTransaction):
        assert await db.scalar(select(func.count()).select_from(model)) == 0

    posts, gets = [], []

    async def create(url, name):
        posts.append(url)
        return {"id": "review-one"}

    async def query(task_id):
        gets.append(task_id)
        return {
            "status": "completed",
            "result": {"usable_assets": [{"asset_url": "asset://one", "status": "Active"}]},
        }

    monkeypatch.setattr(worker.apimart, "create_private_avatar", create)
    monkeypatch.setattr(worker.apimart, "query_generation_task", query)
    await worker._review_image(uploaded.image_id)
    await worker._review_image(uploaded.image_id)
    assert len(posts) == 1 and not gets
    await due(db, uploaded.image_id)
    await worker._review_image(uploaded.image_id)
    await worker._review_image(uploaded.image_id)
    assert gets == ["review-one"]
    await db.refresh(await db.get(SeedanceImage, uploaded.image_id))
    reused = await service.upload_seedance_image(db, user_id, file("renamed.png"))
    assert reused.can_reference and reused.reused and len(calls) == 1
    user_message, assistant, _ = await conversations.send_conversation_message(
        db, conversation_id, data.user, request
    )
    task = await db.get(UserTaskRecord, UUID(assistant.extra["task_record_id"]))
    assert task.extra["user_message_extra"]["image_urls"] == ["asset://one"]
    assert task.prompt == "让@图片1动起来"
    assert user_message.extra["image_references"][0]["url"] == uploaded.url
    assert "asset://" not in str(user_message.extra)


@pytest.mark.asyncio
async def test_scope_ownership_and_legacy_url_bypass_are_rejected(review_setup):
    data, _, _ = review_setup
    db = data.session
    uploaded = await service.upload_seedance_image(db, data.user.id, file())
    with pytest.raises(AppException) as error:
        await service.get_image(db, uuid4(), uploaded.image_id)
    assert error.value.status_code == 404
    model = SimpleNamespace(vendor="apimart", model_type="video", model_id="seedance-2.0")
    for extra in (
        {"image_urls": [uploaded.url]},
        {"image_urls": ["asset://someone-elses"]},
        {"first_frame_url": uploaded.url},
        {"uploaded_images": [uploaded.url]},
    ):
        with pytest.raises(AppException):
            await service.reviewed_video_extra(db, data.user.id, model, "视频", extra)
    assert await service.reviewed_video_extra(db, data.user.id, model, "无图视频", {}) == {}
    record = await db.get(SeedanceImage, uploaded.image_id)
    record.status, record.asset_url = "ready", "asset://one"
    await db.commit()
    result = await service.reviewed_video_extra(
        db, data.user.id, model, "视频", {"first_frame_url": uploaded.url}
    )
    assert result["image_with_roles"] == [{"url": "asset://one", "role": "first_frame"}]
    record.provider_scope = "other-account"
    await db.commit()
    with pytest.raises(AppException):
        await service.reviewed_video_extra(
            db, data.user.id, model, "视频", {"image_urls": [uploaded.url]}
        )


@pytest.mark.asyncio
async def test_uncertain_submission_is_not_automatically_repeated(
    review_setup, monkeypatch, caplog
):
    data, _, _ = review_setup
    uploaded = await service.upload_seedance_image(data.session, data.user.id, file())
    posts = []

    async def timeout(*args):
        posts.append(args)
        raise TimeoutError("connection lost after sending")

    monkeypatch.setattr(worker.apimart, "create_private_avatar", timeout)
    await worker._review_image(uploaded.image_id)
    await worker._review_image(uploaded.image_id)
    record = await data.session.get(SeedanceImage, uploaded.image_id)
    await data.session.refresh(record)
    assert record.status == "uncertain" and record.next_poll_at is None
    assert "响应超时" in service.image_out(record).review_error
    assert f"image_id={uploaded.image_id} phase=submit" in caplog.text
    assert "error_type=TimeoutError" in caplog.text
    assert "connection lost after sending" not in caplog.text
    assert len(posts) == 1
    with pytest.raises(AppException):
        await service.retry_image(data.session, data.user.id, uploaded.image_id)


@pytest.mark.asyncio
async def test_failed_review_requires_explicit_retry_and_beat_recovers_due_work(
    review_setup, monkeypatch
):
    data, calls, _ = review_setup
    uploaded = await service.upload_seedance_image(data.session, data.user.id, file())
    record = await data.session.get(SeedanceImage, uploaded.image_id)
    record.status, record.provider_task_id = "processing", "review-one"
    await data.session.commit()

    async def rejected(task_id):
        return {"status": "failed", "result": {"failed_assets": [{"status": "Failed"}]}}

    monkeypatch.setattr(worker.apimart, "query_generation_task", rejected)
    await worker._review_image(uploaded.image_id)
    await data.session.refresh(record)
    assert record.status == "failed"
    reused = await service.upload_seedance_image(data.session, data.user.id, file())
    assert reused.review_status == "failed" and len(calls) == 1
    retried = await service.retry_image(data.session, data.user.id, uploaded.image_id)
    assert retried.review_status == "pending"
    assert await worker._enqueue_due_reviews(100) == 1


@pytest.mark.asyncio
async def test_upload_http_contract_and_status_authorization(review_setup):
    data, _, _ = review_setup
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(uploads_router, prefix="/api/v1")

    async def database():
        yield data.session

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = lambda: data.user
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/uploads/file",
            data={"purpose": "seedance_reference"},
            files={"file": ("one.png", file().file.getvalue(), "image/png")},
        )
        assert response.status_code == 200
        uploaded = response.json()["data"]
        assert uploaded["review_status"] == "pending" and not uploaded["can_reference"]
        assert "asset_url" not in uploaded and "provider_scope" not in uploaded
        image_id = uploaded["image_id"]
        status = await client.get(f"/api/v1/uploads/images/{image_id}")
        assert status.status_code == 200 and status.json()["data"]["image_id"] == image_id
        record = await data.session.get(SeedanceImage, UUID(image_id))
        record.status = "uncertain"
        record.next_poll_at = None
        await data.session.commit()
        status = (await client.get(f"/api/v1/uploads/images/{image_id}")).json()["data"]
        assert status["can_retry"] and status["retry_requires_confirmation"]
        retry_url = f"/api/v1/uploads/images/{image_id}/retry-review"
        assert (await client.post(retry_url)).status_code == 409
        await data.session.rollback()
        await data.session.refresh(data.user)
        assert (await client.post(retry_url, json={"confirm_resubmit": "true"})).status_code == 422
        response = await client.post(retry_url, json={"confirm_resubmit": True})
        assert response.status_code == 200
        retried = response.json()["data"]
        assert retried["image_id"] == image_id and retried["url"] == uploaded["url"]
        assert retried["review_status"] == "pending" and not retried["can_retry"]
        assert not retried["retry_requires_confirmation"]
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=uuid4())
        assert (await client.get(f"/api/v1/uploads/images/{image_id}")).status_code == 404
        assert (
            await client.post(retry_url, json={"confirm_resubmit": True})
        ).status_code == 404


@pytest.mark.asyncio
async def test_lost_submission_and_query_failure_have_safe_recovery(review_setup, monkeypatch):
    data, _, _ = review_setup
    db = data.session
    uploaded = await service.upload_seedance_image(db, data.user.id, file())
    record = await db.get(SeedanceImage, uploaded.image_id)
    record.status = "submitting"
    await db.commit()
    await worker._review_image(uploaded.image_id)
    await db.refresh(record)
    assert record.status == "uncertain"  # Never resend an unacknowledged POST.

    record.status, record.provider_task_id = "processing", "known-task"
    record.review_started_at = beijing_datetime()
    record.next_poll_at = beijing_datetime()
    await db.commit()

    async def unavailable(task_id):
        raise TimeoutError("temporary query error")

    monkeypatch.setattr(worker.apimart, "query_generation_task", unavailable)
    await worker._review_image(uploaded.image_id)
    await db.refresh(record)
    assert record.status == "processing" and record.next_poll_at > beijing_datetime()
    record.review_started_at = beijing_datetime() - timedelta(minutes=31)
    record.next_poll_at = beijing_datetime()
    await db.commit()
    await worker._review_image(uploaded.image_id)
    await db.refresh(record)
    assert record.status == "uncertain" and record.next_poll_at is None


@pytest.mark.asyncio
async def test_provider_key_change_creates_separate_cache(review_setup, monkeypatch):
    data, calls, _ = review_setup
    first = await service.upload_seedance_image(data.session, data.user.id, file())
    monkeypatch.setattr(settings, "apimart_api_key", "different-test-account")
    second = await service.upload_seedance_image(data.session, data.user.id, file())
    assert first.image_id != second.image_id and len(calls) == 2
    old = await service.get_image(data.session, data.user.id, first.image_id)
    assert not service.image_out(old).can_reference


@pytest.mark.asyncio
async def test_slow_submission_uses_configured_timeout_instead_of_becoming_uncertain(
    review_setup, monkeypatch
):
    data, _, _ = review_setup
    uploaded = await service.upload_seedance_image(data.session, data.user.id, file())
    monkeypatch.setattr(settings, "apimart_timeout_seconds", 600)
    requests = []

    def transport(request):
        requests.append(request)
        # Model a provider acknowledgement after 35 seconds without a wall-clock wait.
        if request.extensions["timeout"]["read"] < 35:
            raise httpx.ReadTimeout("acknowledgement exceeded read timeout", request=request)
        return httpx.Response(200, json={"code": 200, "data": {"id": "slow-review"}})

    client = AsyncOpenAI(
        api_key="test-only",
        base_url="https://example.com/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)),
    )
    monkeypatch.setattr(worker.apimart, "_client", client)
    await worker._review_image(uploaded.image_id)
    record = await data.session.get(SeedanceImage, uploaded.image_id)
    await data.session.refresh(record)
    result = service.image_out(record)
    assert result.review_status == "processing", result.review_error
    assert record.provider_task_id == "slow-review"
    assert len(requests) == 1
    assert requests[0].url.path == "/v1/seedance2/private-avatar"


@pytest.mark.asyncio
async def test_delayed_poll_recovers_completed_review_before_declaring_timeout(
    review_setup, monkeypatch
):
    data, _, _ = review_setup
    uploaded = await service.upload_seedance_image(data.session, data.user.id, file())
    record = await data.session.get(SeedanceImage, uploaded.image_id)
    record.status, record.provider_task_id = "processing", "known-review"
    record.review_started_at = beijing_datetime() - timedelta(hours=4)
    await data.session.commit()
    queries = []

    async def query(task_id):
        queries.append(task_id)
        return {
            "status": "completed",
            "result": {"assets": [{"status": "Active", "asset_url": "asset://approved"}]},
        }

    monkeypatch.setattr(worker.apimart, "query_generation_task", query)
    await worker._review_image(uploaded.image_id)
    await data.session.refresh(record)
    result = service.image_out(record)
    assert result.review_status == "ready", result.review_error
    assert result.can_reference
    assert queries == ["known-review"]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_status, expected", [("processing", "uncertain"), ("failed", "failed")])
async def test_delayed_poll_respects_provider_failure_and_review_deadline(
    review_setup, monkeypatch, provider_status, expected
):
    data, _, _ = review_setup
    uploaded = await service.upload_seedance_image(data.session, data.user.id, file())
    record = await data.session.get(SeedanceImage, uploaded.image_id)
    record.status, record.provider_task_id = "processing", "known-review"
    record.review_started_at = beijing_datetime() - timedelta(minutes=31)
    await data.session.commit()
    queries = []

    async def query(task_id):
        queries.append(task_id)
        return {"status": provider_status}

    monkeypatch.setattr(worker.apimart, "query_generation_task", query)
    await worker._review_image(uploaded.image_id)
    await data.session.refresh(record)
    assert record.status == expected and record.next_poll_at is None
    assert queries == ["known-review"]


@pytest.mark.asyncio
@pytest.mark.parametrize('confirm_resubmit', [False, True])
async def test_uncertain_retry_with_task_id_queries_existing_task(
    review_setup, monkeypatch, confirm_resubmit
):
    data, calls, _ = review_setup
    uploaded = await service.upload_seedance_image(data.session, data.user.id, file())
    record = await data.session.get(SeedanceImage, uploaded.image_id)
    record.status, record.provider_task_id = 'uncertain', 'original-task'
    record.review_started_at = beijing_datetime() - timedelta(hours=4)
    record.next_poll_at = None
    await data.session.commit()
    assert service.image_out(record).can_retry
    assert not service.image_out(record).retry_requires_confirmation
    queries = []

    async def query(task_id):
        queries.append(task_id)
        return {'status': 'completed', 'result': {'asset_url': 'asset://original'}}

    async def unexpected_post(*args):
        pytest.fail('An existing task must not be submitted again')

    monkeypatch.setattr(worker.apimart, 'query_generation_task', query)
    monkeypatch.setattr(worker.apimart, 'create_private_avatar', unexpected_post)
    result = await service.retry_image(
        data.session, data.user.id, uploaded.image_id, confirm_resubmit=confirm_resubmit
    )
    assert result.review_status == 'processing' and not result.can_reference
    assert record.review_started_at > beijing_datetime() - timedelta(minutes=1)
    await worker._review_image(uploaded.image_id)
    await data.session.refresh(record)
    assert queries == ['original-task'] and record.status == 'ready'
    await service.retry_image(data.session, data.user.id, uploaded.image_id, confirm_resubmit=True)
    await worker._review_image(uploaded.image_id)
    assert queries == ['original-task'] and len(calls) == 1


@pytest.mark.asyncio
async def test_confirmed_concurrent_retries_reuse_upload_and_submit_once(review_setup, monkeypatch):
    data, calls, factory = review_setup
    user_id = data.user.id
    uploaded = await service.upload_seedance_image(data.session, user_id, file())
    record = await data.session.get(SeedanceImage, uploaded.image_id)
    record.status = 'uncertain'
    await data.session.commit()
    posts = []

    async def submit(url, name):
        posts.append(url)
        return {'id': 'retried-task'}

    async def retry():
        async with factory() as db:
            return await service.retry_image(db, user_id, uploaded.image_id, confirm_resubmit=True)

    monkeypatch.setattr(worker.apimart, 'create_private_avatar', submit)
    results = await asyncio.gather(retry(), retry())
    assert all(r.review_status == 'pending' and r.image_id == uploaded.image_id for r in results)
    await worker._review_image(uploaded.image_id)
    await worker._review_image(uploaded.image_id)
    assert posts == [uploaded.url] and len(calls) == 1
    assert await data.session.scalar(select(func.count()).select_from(UserPointsTransaction)) == 0
    await data.session.refresh(record)
    record.provider_scope = 'different-provider'
    record.status = 'uncertain'
    await data.session.commit()
    assert not service.image_out(record).can_retry
    assert not service.image_out(record).retry_requires_confirmation
    with pytest.raises(AppException):
        await service.retry_image(data.session, user_id, uploaded.image_id, confirm_resubmit=True)


@pytest.mark.asyncio
async def test_late_submission_response_cannot_overwrite_new_retry_claim(review_setup, monkeypatch):
    data, _, factory = review_setup
    uploaded = await service.upload_seedance_image(data.session, data.user.id, file())

    async def late_response(*args):
        async with factory() as db:
            record = await db.get(SeedanceImage, uploaded.image_id)
            # A replacement attempt is already submitting when the older response arrives.
            record.next_poll_at += timedelta(seconds=1)
            await db.commit()
        return {'id': 'stale-task'}

    monkeypatch.setattr(worker.apimart, 'create_private_avatar', late_response)
    await worker._review_image(uploaded.image_id)
    record = await data.session.get(SeedanceImage, uploaded.image_id)
    await data.session.refresh(record)
    assert record.status == 'submitting' and record.provider_task_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("target_model_id", ["seedance-2.0-fast", "seedance-2.5"])
async def test_switching_seedance_models_keeps_approved_image_and_provider_asset(
    review_setup, monkeypatch, target_model_id
):
    from app.models.ai_model import AiModel

    data, uploads, _ = review_setup
    db = data.session
    data.user.points_balance = 100000
    data.model.vendor, data.model.model_id, data.model.model_type = (
        "apimart", "seedance-2.0", "video"
    )
    data.model.configuration = {"billing": {"base_points": 3}}
    other_model = AiModel(
        nickname="Other Seedance",
        model_id=target_model_id,
        vendor="apimart",
        model_type="video",
        is_enabled=True,
        configuration={"billing": {"base_points": 3}},
    )
    db.add(other_model)
    await db.commit()
    posts, queries = [], []

    async def submit(url, name):
        posts.append(url)
        return {"id": "shared-review"}

    async def query(task_id):
        queries.append(task_id)
        return {"status": "completed", "result": {"asset_url": "asset://shared-image"}}

    monkeypatch.setattr(worker.apimart, "create_private_avatar", submit)
    monkeypatch.setattr(worker.apimart, "query_generation_task", query)
    uploaded = await service.upload_seedance_image(db, data.user.id, file())
    await worker._review_image(uploaded.image_id)
    await due(db, uploaded.image_id)
    await worker._review_image(uploaded.image_id)
    for selected_model in (data.model, other_model, data.model):
        record = await service.get_image(db, data.user.id, uploaded.image_id)
        await db.refresh(record)
        status = service.image_out(record)
        assert status.review_status == "ready" and status.can_reference
        request = ConversationSendMessageRequest(
            ai_model_id=selected_model.id,
            content="让@{图片1}动起来",
            image_references=[{"name": "图片1", "image_id": uploaded.image_id}],
        )
        _, assistant, _ = await conversations.send_conversation_message(
            db, data.conversation.id, data.user, request
        )
        task = await db.get(UserTaskRecord, UUID(assistant.extra["task_record_id"]))
        assert task.ai_model_id == selected_model.id
        assert task.extra["user_message_extra"]["image_urls"] == ["asset://shared-image"]
        await worker._review_image(uploaded.image_id)
    # Even an unnecessary repeated upload must return the approved record unchanged.
    reused = await service.upload_seedance_image(db, data.user.id, file("renamed.png"))
    assert reused.image_id == uploaded.image_id and reused.can_reference and reused.reused
    assert posts == [uploaded.url] and queries == ["shared-review"] and len(uploads) == 1
    assert await db.scalar(select(func.count()).select_from(SeedanceImage)) == 1
