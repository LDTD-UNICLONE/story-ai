# ruff: noqa: F811
import asyncio
import hashlib
import io
import os
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, UploadFile
from sqlalchemy import func, select
from starlette.datastructures import Headers

from app.api.deps import get_current_user
from app.api.v1.endpoints.project_media import router
from app.api.v1.endpoints.project_canvases import router as canvas_router
from app.core.config import settings
from app.core.exceptions import AppException, register_exception_handlers
from app.db.session import get_db
from app.models.project import Project
from app.models.project_media import ProjectMedia
from app.models.seedance_image import SeedanceImage
from app.models.task_dispatch import TaskDispatchOutbox
from app.models.task_record import UserTaskRecord
from app.models.points import UserPointsTransaction
from app.schemas.project_media import ProjectMediaImport
from app.schemas.upload import UploadFileOut
from app.services import seedance_images
from app.services.generation import task_dispatch
from app.services.projects import canvases, media
from tests.test_project_canvases_integration import canvas_ctx, edge, node, patch  # noqa: F401
from tests.test_task_lifecycle_integration import lifecycle_db  # noqa: F401
from tests.test_project_image_reviews_integration import image_download  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.skipif(os.getenv("RUN_DB_INTEGRATION_TESTS") != "1", reason="isolated PostgreSQL"),
]

PNG = b"\x89PNG\r\n\x1a\ncontent"


def image_file(data=PNG):
    return UploadFile(
        file=io.BytesIO(data), filename="image.png", headers=Headers({"content-type": "image/png"})
    )


@pytest.fixture
def storage(monkeypatch):
    calls = []

    async def upload(file, **kwargs):
        calls.append(kwargs)
        return UploadFileOut(
            url=f"https://example.com/{uuid4()}.png",
            object_key="image.png",
            filename=file.filename,
            content_type="image/png",
            size=len(PNG),
            file_type="image",
        )

    monkeypatch.setattr(media, "upload_story_file", upload)
    monkeypatch.setattr(settings, "apimart_api_key", "test-media-review")
    monkeypatch.setattr(task_dispatch, "publish_task_message", lambda **kwargs: None)
    return calls


async def uploaded(ctx, data=PNG):
    return await media.upload_image(ctx.db, ctx.pid, ctx.uid, image_file(data))


async def test_concurrent_duplicate_upload_stores_once(canvas_ctx, storage):
    ctx = canvas_ctx

    async def submit():
        async with ctx.factory() as db:
            return await media.upload_image(db, ctx.pid, ctx.uid, image_file())

    a, b = await asyncio.gather(submit(), submit())
    assert a.id == b.id and a.url == b.url and len(storage) == 1
    assert a.review is None
    assert await ctx.db.scalar(select(func.count()).select_from(ProjectMedia)) == 1
    for table in (TaskDispatchOutbox, UserTaskRecord, UserPointsTransaction):
        assert await ctx.db.scalar(select(func.count()).select_from(table)) == 0


async def test_invalid_image_and_foreign_project_rejected_before_storage(canvas_ctx, storage):
    ctx = canvas_ctx
    for pid, uid, data in [
        (ctx.pid, uuid4(), PNG),
        (uuid4(), ctx.uid, PNG),
        (ctx.pid, ctx.uid, b"not image"),
    ]:
        with pytest.raises(AppException):
            await media.upload_image(ctx.db, pid, uid, image_file(data))
    assert not storage


async def test_upload_reuses_existing_review_file_and_import_identity(canvas_ctx, storage):
    ctx = canvas_ctx
    record = SeedanceImage(
        user_id=ctx.uid,
        provider_scope=seedance_images.provider_scope(),
        sha256=hashlib.sha256(PNG).hexdigest(),
        status="ready",
        asset_url="asset://ready",
        upload=UploadFileOut(
            url="https://example.com/reviewed.png",
            object_key="old.png",
            filename="old.png",
            content_type="image/png",
            size=len(PNG),
            file_type="image",
        ).model_dump(),
    )
    ctx.db.add(record)
    await ctx.db.commit()
    result = await uploaded(ctx)
    assert result.review.can_reference and result.review.image_id == record.id
    assert not storage
    payload = ProjectMediaImport(source_type="seedance_image", source_id=record.id)
    a = await media.import_image(ctx.db, ctx.pid, ctx.uid, payload)
    b = await media.import_image(ctx.db, ctx.pid, ctx.uid, payload)
    assert a.id == b.id and a.url == result.url
    assert (await media.review_media(ctx.db, ctx.pid, ctx.uid, a.id)).image_id == record.id
    assert await ctx.db.scalar(select(func.count()).select_from(TaskDispatchOutbox)) == 0


@pytest.mark.parametrize("source_type", ["generated_image", "generated_last_frame"])
async def test_removed_legacy_import_types_are_rejected(canvas_ctx, source_type):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ProjectMediaImport(source_type=source_type, source_id=uuid4())


async def test_review_reuses_bytes_aliases_and_refreshes_without_model_binding(
    canvas_ctx, storage, image_download
):  # noqa: F811
    ctx = canvas_ctx
    first, second = await uploaded(ctx), await uploaded(ctx, PNG + b"different")
    review = await media.review_media(ctx.db, ctx.pid, ctx.uid, first.id)
    assert review.review_status == "pending" and len(storage) == 2 and len(image_download) == 1
    record = await ctx.db.get(SeedanceImage, review.image_id)
    record.status, record.asset_url = "ready", "asset://ready"
    await ctx.db.commit()
    again = await media.review_media(ctx.db, ctx.pid, ctx.uid, first.id)
    assert again.image_id == review.image_id and again.can_reference and len(image_download) == 1
    # Mock download returns identical bytes at two stored URLs: content review is shared.
    alias = await media.review_media(ctx.db, ctx.pid, ctx.uid, second.id)
    assert alias.image_id == review.image_id and alias.can_reference and len(image_download) == 2
    listing = await media.list_media(ctx.db, ctx.pid, ctx.uid, 1, 20)
    assert all(item["review"]["can_reference"] for item in listing["items"])
    assert await ctx.db.scalar(select(func.count()).select_from(SeedanceImage)) == 1
    assert len(storage) == 2


async def test_no_provider_does_not_break_editor(canvas_ctx, storage, monkeypatch):
    ctx = canvas_ctx
    monkeypatch.setattr(settings, "apimart_api_key", "")
    result = await uploaded(ctx)
    assert result.review is None
    assert (await media.list_media(ctx.db, ctx.pid, ctx.uid, 1, 20))["items"][0]["review"] is None
    with pytest.raises(AppException) as error:
        await media.review_media(ctx.db, ctx.pid, ctx.uid, result.id)
    assert error.value.code == 50021


async def test_changed_provider_scope_does_not_reuse_old_approval(
    canvas_ctx,
    storage,
    image_download,
    monkeypatch,
):
    ctx = canvas_ctx
    result = await uploaded(ctx)
    review = await media.review_media(ctx.db, ctx.pid, ctx.uid, result.id)
    record = await ctx.db.get(SeedanceImage, review.image_id)
    record.status, record.asset_url = "ready", "asset://old-scope"
    await ctx.db.commit()
    monkeypatch.setattr(settings, "apimart_api_key", "different-provider-account")
    listing = await media.list_media(ctx.db, ctx.pid, ctx.uid, 1, 20)
    assert listing["items"][0]["review"] is None
    # A read never submits a review under the new account.
    assert len(image_download) == 1
    new_review = await media.review_media(ctx.db, ctx.pid, ctx.uid, result.id)
    assert new_review.image_id != review.image_id and not new_review.can_reference
    assert len(storage) == 1


async def test_foreign_review_id_and_disabled_project_rejected(canvas_ctx, storage, image_download):
    ctx = canvas_ctx
    result = await uploaded(ctx)
    review = await media.review_media(ctx.db, ctx.pid, ctx.uid, result.id)
    from app.models.user import User

    stranger = User(account=uuid4().hex, password_hash="test", nickname="Other")
    ctx.db.add(stranger)
    await ctx.db.flush()
    other = Project(user_id=stranger.id, name="Other", cover="", description="")
    ctx.db.add(other)
    await ctx.db.commit()
    with pytest.raises(AppException):
        await media.import_image(
            ctx.db,
            other.id,
            stranger.id,
            ProjectMediaImport(source_type="seedance_image", source_id=review.image_id),
        )
    project = await ctx.db.get(Project, ctx.pid)
    project.is_enabled = False
    await ctx.db.commit()
    with pytest.raises(AppException):
        await media.review_media(ctx.db, ctx.pid, ctx.uid, result.id)
    assert len(image_download) == 1


async def test_canvas_pin_is_immutable_until_explicit_refresh(canvas_ctx, storage):
    ctx = canvas_ctx
    first, second = await uploaded(ctx), await uploaded(ctx, PNG + b"2")
    a, b, unused = node(media_id=first.id), node("video"), node(media_id=second.id)
    link = edge(a, b)
    saved = await patch(ctx, upsert_nodes=[a, b, unused], upsert_edges=[link])
    assert saved.edges[0].media_id == first.id
    a.media_id = second.id
    saved = await patch(ctx, 2, upsert_nodes=[a])
    assert saved.edges[0].media_id == first.id
    assert {n.id: n.content_revision for n in saved.nodes} == {a.id: 2, b.id: 1, unused.id: 1}
    inputs = await canvases.image_inputs(ctx.db, ctx.pid, ctx.uid, ctx.cid, b.id)
    assert len(inputs["items"]) == 1
    assert inputs["items"][0]["media_id"] == str(first.id)
    assert inputs["items"][0]["source_changed"] and inputs["items"][0]["available"]
    saved = await patch(ctx, 3, upsert_edges=[link])  # omitted media_id preserves the pin
    assert saved.edges[0].media_id == first.id
    link.media_id = second.id
    saved = await patch(ctx, 4, upsert_edges=[link])
    assert saved.edges[0].media_id == second.id
    assert next(n for n in saved.nodes if n.id == b.id).content_revision == 2
    await patch(ctx, 5, delete_node_ids=[a.id])
    inputs = await canvases.image_inputs(ctx.db, ctx.pid, ctx.uid, ctx.cid, b.id)
    assert not inputs["items"]
    assert await ctx.db.scalar(select(func.count()).select_from(ProjectMedia)) == 2


@pytest.mark.parametrize("case", ["foreign_media", "wrong_kind", "forged_pin", "non_image_edge"])
async def test_invalid_canvas_media_is_atomic(canvas_ctx, storage, case):
    ctx = canvas_ctx
    first, second = await uploaded(ctx), await uploaded(ctx, PNG + b"2")
    a, b = node(media_id=first.id), node("video")
    link = edge(a, b)
    if case == "foreign_media":
        other = Project(user_id=ctx.uid, name="Other", cover="", description="")
        ctx.db.add(other)
        await ctx.db.commit()
        foreign = await media.upload_image(ctx.db, other.id, ctx.uid, image_file())
        a.media_id = foreign.id
    elif case == "wrong_kind":
        b.media_id = first.id
    elif case == "forged_pin":
        link.media_id = second.id
    else:
        a.kind, a.media_id, link.media_id = "text", None, first.id
    with pytest.raises(AppException):
        await patch(ctx, upsert_nodes=[a, b], upsert_edges=[link])
    await ctx.db.commit()
    saved = await canvases.get_canvas(ctx.db, ctx.pid, ctx.uid, ctx.cid)
    assert saved.revision == 1 and not saved.nodes and not saved.edges


async def test_empty_source_can_be_connected_but_not_used(canvas_ctx, storage):
    ctx = canvas_ctx
    a, b = node(), node("video")
    link = edge(a, b, input="first_frame")
    await patch(ctx, upsert_nodes=[a, b], upsert_edges=[link])
    inputs = await canvases.image_inputs(ctx.db, ctx.pid, ctx.uid, ctx.cid, b.id)
    assert not inputs["items"][0]["available"]
    first = await uploaded(ctx)
    a.media_id = first.id
    await patch(ctx, 2, upsert_nodes=[a])
    inputs = await canvases.image_inputs(ctx.db, ctx.pid, ctx.uid, ctx.cid, b.id)
    assert not inputs["items"][0]["available"] and inputs["items"][0]["source_changed"]
    link.media_id = None  # explicit null refreshes to the current source
    await patch(ctx, 3, upsert_edges=[link])
    inputs = await canvases.image_inputs(ctx.db, ctx.pid, ctx.uid, ctx.cid, b.id)
    assert inputs["items"][0]["available"] and inputs["items"][0]["input"] == "first_frame"


async def test_media_http_upload_import_list_and_input_contract(canvas_ctx, storage):
    ctx = canvas_ctx
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.include_router(canvas_router, prefix="/api/v1")
    register_exception_handlers(app)

    async def session():
        async with ctx.factory() as db:
            yield db

    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=ctx.uid)
    path = f"/api/v1/projects/{ctx.pid}/media"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            path + "/images", files={"file": ("image.png", PNG, "image/png")}
        )
        assert response.status_code == 200
        result = response.json()["data"]
        assert (await client.get(path + "/" + result["id"])).json()["data"] == result
        assert (await client.get(path)).json()["data"]["total"] == 1
        assert (
            await client.post(
                path + "/imports",
                json={"source_type": "url", "source_id": str(uuid4()), "url": "http://internal"},
            )
        ).status_code == 422
        assert (await client.get(path + "/" + str(uuid4()))).status_code == 404
        missing = f"/api/v1/projects/{ctx.pid}/canvases/{ctx.cid}/nodes/{uuid4()}/image-inputs"
        assert (await client.get(missing)).status_code == 404


async def test_concurrent_media_reviews_share_content_without_reupload(canvas_ctx, storage, image_download):
    ctx = canvas_ctx
    a, b = await uploaded(ctx), await uploaded(ctx, PNG + b"different")

    async def review(mid):
        async with ctx.factory() as db:
            return await media.review_media(db, ctx.pid, ctx.uid, mid)

    first, second = await asyncio.gather(review(a.id), review(b.id))
    assert first.image_id == second.image_id
    assert len(image_download) == 2 and len(storage) == 2
    assert await ctx.db.scalar(select(func.count()).select_from(SeedanceImage)) == 1
    aliases = await seedance_images.images_by_urls(ctx.db, ctx.uid, {a.url, b.url})
    assert set(aliases) == {a.url, b.url}


@pytest.mark.parametrize("invalid", ["not-image", "oversize", "unreachable"])
async def test_media_read_failure_creates_no_review_or_charge(canvas_ctx, storage, monkeypatch, invalid):
    from app.services.projects import image_reviews

    ctx = canvas_ctx
    picture = await uploaded(ctx)
    monkeypatch.setattr(settings, "max_upload_size_mb", 1)

    async def read(client, url, **kwargs):
        if invalid == "unreachable":
            raise httpx.ConnectError("unreachable")
        content = b"not an image" if invalid == "not-image" else b"x" * (1024 * 1024 + 1)
        return httpx.Response(200, content=content, request=httpx.Request("GET", url))

    monkeypatch.setattr(image_reviews, "open_safe_http_response", read)
    with pytest.raises(AppException):
        await media.review_media(ctx.db, ctx.pid, ctx.uid, picture.id)
    for table in (SeedanceImage, TaskDispatchOutbox, UserTaskRecord, UserPointsTransaction):
        assert await ctx.db.scalar(select(func.count()).select_from(table)) == 0
