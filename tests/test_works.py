from datetime import datetime
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import UploadFile

from app.api.v1.endpoints import works as works_endpoint
from app.core.config import settings
from app.core.exceptions import AppException
from app.db.session import get_db
from app.integrations.oss import OssClient
from app.main import app
from app.services import material_streaming, work_streaming, works as works_service
from app.services.works import _ensure_can_view_work, _media_out, _work_upload_preview_url


def test_work_media_output_uses_authenticated_application_urls() -> None:
    work_id = uuid4()
    media_id = uuid4()
    media = SimpleNamespace(
        id=media_id,
        media_type="video",
        url="https://public-oss.example/secret.mp4",
        filename="secret.mp4",
        content_type="video/mp4",
        size=123,
        width=None,
        height=None,
        duration_seconds=None,
        sort_order=0,
        thumbnail_object_key=None,
        created_at=datetime.now(),
    )

    output = _media_out(work_id, media)

    expected = f"{settings.api_prefix}/works/{work_id}/media/{media_id}/stream"
    assert output.url == expected
    assert output.stream_url == expected
    assert output.thumbnail_url is None
    assert "public-oss.example" not in output.model_dump_json()


def test_work_media_output_includes_thumbnail_url_when_thumbnail_exists() -> None:
    work_id = uuid4()
    media_id = uuid4()
    media = SimpleNamespace(
        id=media_id,
        media_type="video",
        url="https://public-oss.example/secret.mp4",
        filename="secret.mp4",
        content_type="video/mp4",
        size=123,
        width=None,
        height=None,
        duration_seconds=None,
        sort_order=0,
        thumbnail_object_key="works/thumbnail.jpg",
        created_at=datetime.now(),
    )

    output = _media_out(work_id, media)

    assert output.thumbnail_url == (
        f"{settings.api_prefix}/works/{work_id}/media/{media_id}/thumbnail"
    )


@pytest.mark.asyncio
async def test_work_media_lookup_uses_one_database_query() -> None:
    work_id = uuid4()
    media_id = uuid4()
    work = SimpleNamespace(
        is_enabled=True,
        user_id=uuid4(),
        status="published",
        visibility="public",
    )
    media = SimpleNamespace(id=media_id, work_id=work_id)

    class FakeResult:
        def one_or_none(self):
            return work, media

    class FakeDb:
        def __init__(self) -> None:
            self.execute_count = 0

        async def execute(self, _statement):
            self.execute_count += 1
            return FakeResult()

    db = FakeDb()
    result = await works_service.get_work_media_for_stream(
        db, work_id, media_id, user=None
    )

    assert result is media
    assert db.execute_count == 1


def test_work_upload_preview_uses_authenticated_application_url() -> None:
    upload_id = uuid4()
    assert _work_upload_preview_url(upload_id) == (
        f"{settings.api_prefix}/works/uploads/{upload_id}/preview"
    )


@pytest.mark.asyncio
async def test_public_work_list_returns_one_preview_and_full_media_count() -> None:
    work_id = uuid4()
    media_id = uuid4()
    now = datetime.now()
    work = SimpleNamespace(
        id=work_id,
        user_id=uuid4(),
        title="作品",
        description=None,
        visibility="public",
        status="published",
        like_count=3,
        view_count=5,
        is_enabled=True,
        created_at=now,
        updated_at=now,
    )
    media = SimpleNamespace(
        id=media_id,
        work_id=work_id,
        media_type="video",
        filename="preview.mp4",
        content_type="video/mp4",
        size=123,
        width=None,
        height=None,
        duration_seconds=5,
        sort_order=0,
        thumbnail_object_key=None,
        created_at=now,
    )

    class FakeResult:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    class FakeDb:
        def __init__(self) -> None:
            self.results = [FakeResult([(work, 1)]), FakeResult([(media, 4)])]
            self.execute_count = 0

        async def execute(self, _statement):
            result = self.results[self.execute_count]
            self.execute_count += 1
            return result

    db = FakeDb()
    items, total = await works_service.list_public_works(db, None, 1, 20)

    assert total == 1
    assert db.execute_count == 2
    assert len(items) == 1
    assert items[0].media_count == 4
    assert [item.id for item in items[0].media_items] == [media_id]


@pytest.mark.asyncio
async def test_public_work_list_preserves_total_for_empty_later_page() -> None:
    class PageResult:
        def all(self):
            return []

    class CountResult:
        def scalar_one(self):
            return 42

    class FakeDb:
        def __init__(self) -> None:
            self.results = [PageResult(), CountResult()]
            self.execute_count = 0

        async def execute(self, _statement):
            result = self.results[self.execute_count]
            self.execute_count += 1
            return result

    db = FakeDb()
    works, total = await works_service._query_public_works_with_total(
        db, [], page=4, page_size=20
    )

    assert works == []
    assert total == 42
    assert db.execute_count == 2


@pytest.mark.asyncio
async def test_work_upload_sets_private_object_acl(monkeypatch) -> None:
    captured = {}

    class FakeBucket:
        def put_object(self, object_key, file_obj, *, headers):
            captured["object_key"] = object_key
            captured["headers"] = headers
            captured["body"] = file_obj.read()
            return SimpleNamespace(status=200)

    class FakeOssClient:
        def __init__(self):
            self.bucket = FakeBucket()

        def public_url(self, object_key):
            return f"https://bucket.example/{object_key}"

    class FakeDb:
        def add(self, _record):
            return None

        async def commit(self):
            return None

        async def refresh(self, _record):
            return None

    async def no_op(*_args):
        return None

    monkeypatch.setattr(works_service, "OssClient", FakeOssClient)
    monkeypatch.setattr(works_service, "_lock_user_work_storage", no_op)
    monkeypatch.setattr(works_service, "_enforce_user_work_storage_limit", no_op)
    user = SimpleNamespace(id=uuid4())
    file = UploadFile(
        filename="cover.png",
        file=BytesIO(b"\x89PNG\r\n\x1a\nimage-data"),
    )

    await works_service.upload_work_file(FakeDb(), user, file)

    assert captured["object_key"].startswith("story/works/")
    assert captured["headers"] == {
        "Content-Type": "image/png",
        "x-oss-object-acl": "private",
    }


def test_oss_signed_download_url_uses_configured_expiry(monkeypatch) -> None:
    class FakeBucket:
        def sign_url(self, method, key, expires, *, params, slash_safe):
            assert (method, key, expires) == ("GET", "works/video.mp4", 321)
            assert params == {
                "response-content-disposition": "inline; filename*=UTF-8''video.mp4"
            }
            assert slash_safe is True
            return "https://bucket.example/signed"

    client = object.__new__(OssClient)
    client.bucket = FakeBucket()
    monkeypatch.setattr(settings, "oss_signed_url_expires_seconds", 321)

    assert client.signed_download_url("works/video.mp4", "video.mp4") == (
        "https://bucket.example/signed"
    )


def test_anonymous_user_can_view_published_public_work() -> None:
    work = SimpleNamespace(
        is_enabled=True,
        user_id=uuid4(),
        status="published",
        visibility="public",
    )

    _ensure_can_view_work(work, None)


def test_anonymous_user_cannot_view_private_work() -> None:
    work = SimpleNamespace(
        is_enabled=True,
        user_id=uuid4(),
        status="published",
        visibility="private",
    )

    with pytest.raises(AppException) as exc_info:
        _ensure_can_view_work(work, None)

    assert exc_info.value.status_code == 403


def test_public_work_reads_are_anonymous_but_likes_require_login() -> None:
    paths = app.openapi()["paths"]
    prefix = settings.api_prefix

    assert paths[f"{prefix}/works"]["get"].get("security") is None
    assert paths[f"{prefix}/works/{{work_id}}"]["get"].get("security") is None
    assert (
        paths[f"{prefix}/works/{{work_id}}/media/{{media_id}}/stream"]["get"].get(
            "security"
        )
        is None
    )
    assert paths[f"{prefix}/works/{{work_id}}/like"]["post"]["security"]
    assert paths[f"{prefix}/works/{{work_id}}/like"]["delete"]["security"]


@pytest.mark.asyncio
async def test_anonymous_list_succeeds_but_anonymous_like_is_rejected(monkeypatch) -> None:
    async def override_db():
        yield SimpleNamespace()

    async def list_public_works(_db, user, page, page_size):
        assert user is None
        assert (page, page_size) == (1, 20)
        return [], 0

    app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr(works_endpoint, "list_public_works", list_public_works)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            list_response = await client.get(f"{settings.api_prefix}/works")
            like_response = await client.post(
                f"{settings.api_prefix}/works/{uuid4()}/like"
            )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert list_response.status_code == 200
    assert like_response.status_code == 401


@pytest.mark.asyncio
async def test_work_media_stream_releases_db_before_starting_stream(monkeypatch) -> None:
    class TrackingDb:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    db = TrackingDb()
    media = object()
    response = object()

    async def get_media(_db, _work_id, _media_id, _user):
        assert _db is db
        return media

    async def start_stream(_media):
        assert _media is media
        assert db.closed is True
        return response

    monkeypatch.setattr(works_endpoint, "get_work_media_for_stream", get_media)
    monkeypatch.setattr(works_endpoint, "stream_work_media", start_stream)

    result = await works_endpoint.work_media_stream(
        work_id=uuid4(),
        media_id=uuid4(),
        db=db,
        current_user=None,
    )

    assert result is response


@pytest.mark.asyncio
async def test_work_media_redirects_to_signed_oss_url(monkeypatch) -> None:
    class FakeOssClient:
        def signed_download_url(self, object_key, filename):
            assert object_key == "works/video.mp4"
            assert filename == "video.mp4"
            return "https://bucket.example/works/video.mp4?signature=temporary"

    monkeypatch.setattr(work_streaming, "OssClient", FakeOssClient)
    media = SimpleNamespace(
        object_key="works/video.mp4",
        filename="video.mp4",
        content_type="video/mp4",
        size=1024,
    )

    response = await work_streaming.stream_work_media(media)

    assert response.status_code == 307
    assert response.headers["location"] == (
        "https://bucket.example/works/video.mp4?signature=temporary"
    )


@pytest.mark.asyncio
async def test_material_image_redirects_to_signed_oss_url(monkeypatch) -> None:
    class FakeOssClient:
        def signed_download_url(self, object_key, filename):
            assert object_key == "materials/image.png"
            assert filename == "image.png"
            return "https://bucket.example/materials/image.png?signature=temporary"

    monkeypatch.setattr(material_streaming, "OssClient", FakeOssClient)
    material = SimpleNamespace(
        image_object_key="materials/image.png",
        filename="image.png",
        content_type="image/png",
        size=1024,
    )

    response = await material_streaming.stream_material_image(material)

    assert response.status_code == 307
    assert response.headers["location"] == (
        "https://bucket.example/materials/image.png?signature=temporary"
    )
