import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.core.exceptions import AppException
from app.models.ai_model import AiModel
from app.models.points import UserPointsTransaction
from app.models.project import Project
from app.models.project_storyboard import ProjectStoryboard
from app.models.seedance_image import SeedanceImage
from app.models.style import Style
from app.models.task_record import UserTaskRecord
from app.models.user import User
from app.schemas.project_storyboard import ProjectStoryboardVideoGenerateRequest
from app.services import seedance_images
from app.services.generation import task_dispatch
from app.services.projects import storyboard_videos
from tests.test_task_lifecycle_integration import _seed_task, lifecycle_db  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.skipif(os.getenv("RUN_DB_INTEGRATION_TESTS") != "1", reason="isolated PostgreSQL"),
]


@pytest.fixture
async def project_review(lifecycle_db, monkeypatch):  # noqa: F811
    seed = await _seed_task(lifecycle_db, "storyboard_video")
    monkeypatch.setattr(settings, "apimart_api_key", "test-project-review")
    monkeypatch.setattr(task_dispatch, "publish_task_message", lambda **kwargs: None)
    async with lifecycle_db() as db:
        user = await db.get(User, seed.user_id)
        user.points_balance = 100000
        model = await db.get(AiModel, seed.model_id)
        model.vendor, model.model_id = "apimart", "seedance-2.0"
        style = Style(name=str(uuid4()), cover="", prompt="test")
        db.add(style)
        await db.flush()
        project = await db.get(Project, seed.project_id)
        project.style_id = style.id
        await db.commit()
        yield SimpleNamespace(db=db, seed=seed, user=user, model=model, factory=lifecycle_db)


def upload_data(url="https://example.com/reference.png"):
    return dict(
        url=url,
        object_key="test/image.png",
        filename="image.png",
        content_type="image/png",
        size=20,
        file_type="image",
    )


async def review_record(ctx, status="ready", url="https://example.com/reference.png"):
    record = SeedanceImage(
        user_id=ctx.seed.user_id,
        provider_scope=seedance_images.provider_scope(),
        sha256=uuid4().hex,
        upload=upload_data(url),
        status=status,
        asset_url="asset://reviewed" if status == "ready" else None,
    )
    ctx.db.add(record)
    await ctx.db.commit()
    return record


async def submit(ctx, payload):
    return await storyboard_videos.submit_storyboard_video_generation(
        ctx.db, ctx.seed.project_id, ctx.seed.chapter_id, ctx.seed.storyboard_id, ctx.user, payload
    )


@pytest.mark.parametrize("mode", ["reference", "first_last_frame", "storyboard"])
@pytest.mark.parametrize("status", ["unregistered", "pending", "processing", "failed", "uncertain"])
async def test_project_rejects_unreviewed_images_before_task_or_charge(
    project_review, mode, status
):
    ctx = project_review
    url = upload_data()["url"]
    if status != "unregistered":
        await review_record(ctx, status)
    sb = await ctx.db.get(ProjectStoryboard, ctx.seed.storyboard_id)
    sb.video_prompt = "Test video"
    sb.extra = {"image_generation_result": url}
    await ctx.db.commit()
    payload = ProjectStoryboardVideoGenerateRequest(
        ai_model_id=ctx.seed.model_id,
        generation_mode=mode,
        uploaded_images=[url] if mode == "reference" else [],
        first_frame_url=url if mode == "first_last_frame" else None,
    )
    before = await ctx.db.scalar(select(func.count()).select_from(UserTaskRecord))
    with pytest.raises(AppException) as error:
        await submit(ctx, payload)
    assert error.value.code == 40016
    assert await ctx.db.scalar(select(func.count()).select_from(UserTaskRecord)) == before
    assert await ctx.db.scalar(select(func.count()).select_from(UserPointsTransaction)) == 0


@pytest.mark.parametrize("model_id", ["seedance-2.0", "seedance-2.0-fast", "seedance-2.5"])
@pytest.mark.parametrize("mode", ["reference", "first_last_frame", "storyboard"])
async def test_reviewed_project_images_convert_without_losing_preview(
    project_review, model_id, mode
):
    ctx = project_review
    record = await review_record(ctx)
    ctx.model.model_id = model_id
    sb = await ctx.db.get(ProjectStoryboard, ctx.seed.storyboard_id)
    sb.video_prompt = "Test video"
    sb.extra = {"image_generation_result": record.upload["url"]}
    await ctx.db.commit()
    payload = ProjectStoryboardVideoGenerateRequest(
        ai_model_id=ctx.seed.model_id,
        generation_mode=mode,
        uploaded_images=[record.upload["url"]] if mode == "reference" else [],
        first_frame_url=record.upload["url"] if mode == "first_last_frame" else None,
    )
    task, _ = await submit(ctx, payload)
    extra = task.extra["model_extra"]
    if mode == "first_last_frame":
        assert extra["image_with_roles"] == [{"url": "asset://reviewed", "role": "first_frame"}]
        assert task.extra["first_frame_url"] == record.upload["url"]
    else:
        assert extra["image_urls"] == ["asset://reviewed"]
        assert task.extra["reference_images"] == [record.upload["url"]]
    assert record.upload["url"].startswith("https://")
    assert await ctx.db.scalar(select(func.count()).select_from(SeedanceImage)) == 1


@pytest.mark.parametrize(
    "case", ["foreign_user", "changed_scope", "asset_url", "unreviewed_last_frame"]
)
async def test_project_review_bypass_is_rejected(project_review, case):
    ctx = project_review
    record = await review_record(ctx)
    url = record.upload["url"]
    if case == "foreign_user":
        stranger = User(account=uuid4().hex, password_hash="test", nickname="other")
        ctx.db.add(stranger)
        await ctx.db.flush()
        record.user_id = stranger.id
    if case == "changed_scope":
        record.provider_scope = "old-provider"
    await ctx.db.commit()
    payload = ProjectStoryboardVideoGenerateRequest(
        ai_model_id=ctx.seed.model_id,
        generation_mode="first_last_frame" if case == "unreviewed_last_frame" else "reference",
        uploaded_images=["asset://reviewed" if case == "asset_url" else url],
        first_frame_url=url if case == "unreviewed_last_frame" else None,
        last_frame_url="https://example.com/not-reviewed.png"
        if case == "unreviewed_last_frame"
        else None,
    )
    with pytest.raises(AppException) as exc:
        await submit(ctx, payload)
    assert exc.value.code == 40016 and exc.value.data["images"]
    assert await ctx.db.scalar(select(func.count()).select_from(UserPointsTransaction)) == 0


async def test_text_only_and_other_providers_do_not_require_seedance_review(project_review):
    ctx = project_review
    task, _ = await submit(
        ctx,
        ProjectStoryboardVideoGenerateRequest(
            ai_model_id=ctx.seed.model_id, generation_mode="text_to_video"
        ),
    )
    assert not task.extra["model_extra"].get("image_urls")
    ctx.model.vendor, ctx.model.model_id = "comfly", "wan-2.1"
    await ctx.db.commit()
    task, _ = await submit(
        ctx,
        ProjectStoryboardVideoGenerateRequest(
            ai_model_id=ctx.seed.model_id, uploaded_images=["https://example.com/plain.png"]
        ),
    )
    assert task.extra["model_extra"]["image_urls"] == ["https://example.com/plain.png"]


@pytest.fixture
def image_download(monkeypatch):
    import httpx
    from app.services.projects import image_reviews

    calls = []

    async def read(client, url, **kwargs):
        calls.append(url)
        return httpx.Response(
            200, content=b"\x89PNG\r\n\x1a\ncontent", request=httpx.Request("GET", url)
        )

    async def forbidden_upload(*args, **kwargs):
        pytest.fail("An existing generated file must not be uploaded to OSS again")

    monkeypatch.setattr(image_reviews, "open_safe_http_response", read)
    monkeypatch.setattr(seedance_images, "upload_story_file", forbidden_upload)
    return calls


async def test_removed_project_review_http_contract(project_review):
    import httpx
    from fastapi import FastAPI
    from app.api.v1.router import api_router

    app = FastAPI()
    app.include_router(api_router, prefix="/api/v1")
    path = f"/api/v1/projects/{project_review.seed.project_id}/image-reviews"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get(path)).status_code == 404
        assert (await client.post(path, json={})).status_code == 404
