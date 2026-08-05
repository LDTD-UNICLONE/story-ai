from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.announcement_security import (
    sanitize_announcement_content,
    validate_announcement_url,
)
from app.core.exceptions import AppException
from app.schemas.announcement import (
    AnnouncementBaseOut,
    AnnouncementCreateRequest,
    AnnouncementUpdateRequest,
)
from app.services import announcements


class FakeSession:
    def __init__(self) -> None:
        self.added = None
        self.committed = False

    def add(self, value) -> None:
        self.added = value

    async def commit(self) -> None:
        self.committed = True

    async def refresh(self, value) -> None:
        return None


def test_html_content_is_cleaned_with_safe_formatting_preserved() -> None:
    content = (
        '<p onclick="alert(1)"><strong>通知</strong>'
        '<a href="javascript:alert(1)">危险链接</a>'
        '<a href="https://example.com/detail">安全链接</a>'
        '<img src="https://example.com/a.png" onerror="alert(1)"></p>'
        '<script>alert(1)</script>'
    )

    cleaned = sanitize_announcement_content(content, "html")

    assert "<strong>通知</strong>" in cleaned
    assert 'href="https://example.com/detail"' in cleaned
    assert 'src="https://example.com/a.png"' in cleaned
    assert "javascript:" not in cleaned
    assert "onclick" not in cleaned
    assert "onerror" not in cleaned
    assert "<script" not in cleaned
    assert "alert(1)" not in cleaned


@pytest.mark.parametrize(
    "content",
    [
        "[点击](javascript:alert(1))",
        "[点击](java&#x73;cript:alert(1))",
        "[点击](%6a%61%76%61%73%63%72%69%70%74:alert(1))",
        "![图片](data:image/svg+xml;base64,PHN2Zz4=)",
    ],
)
def test_markdown_rejects_dangerous_schemes(content: str) -> None:
    with pytest.raises(AppException, match="不安全链接"):
        sanitize_announcement_content(content, "markdown")


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html;base64,AAAA",
        "//example.com/image.png",
        "https://user:secret@example.com/image.png",
        "https://example.com\n.evil.test/image.png",
    ],
)
def test_announcement_url_rejects_unsafe_values(url: str) -> None:
    with pytest.raises(AppException) as exc_info:
        validate_announcement_url(url, "公告图片地址")

    assert exc_info.value.code == 40021


def test_announcement_url_accepts_absolute_http_urls_and_normalizes_blank() -> None:
    assert (
        validate_announcement_url(" https://example.com/a.png?size=2 ", "公告图片地址")
        == "https://example.com/a.png?size=2"
    )
    assert validate_announcement_url("   ", "公告跳转地址") is None


@pytest.mark.asyncio
async def test_create_announcement_sanitizes_before_persisting() -> None:
    db = FakeSession()
    payload = AnnouncementCreateRequest(
        title="系统通知",
        content='<p onclick="alert(1)">正文</p><script>alert(1)</script>',
        content_format="html",
        image_url="https://example.com/image.png",
    )

    created = await announcements.create_announcement(db, payload)

    assert db.committed is True
    assert created is db.added
    assert created.content == "<p>正文</p>"


@pytest.mark.asyncio
async def test_update_format_resanitizes_existing_content(monkeypatch) -> None:
    existing = SimpleNamespace(
        content='<p onclick="alert(1)">正文</p>',
        content_format="plain",
        image_url=None,
        link_url=None,
        start_at=None,
        end_at=None,
        updated_at=None,
    )
    db = FakeSession()

    async def get_existing(_db, _announcement_id):
        return existing

    monkeypatch.setattr(announcements, "get_announcement_or_404", get_existing)

    await announcements.update_announcement(
        db,
        uuid4(),
        AnnouncementUpdateRequest(content_format="html"),
    )

    assert existing.content_format == "html"
    assert existing.content == "<p>正文</p>"


def test_response_sanitizes_legacy_unsafe_fields() -> None:
    response = AnnouncementBaseOut.model_validate(
        {
            "id": uuid4(),
            "title": "历史公告",
            "content": '<img src="x" onerror="alert(1)"><p>正文</p>',
            "content_format": "html",
            "image_url": "javascript:alert(1)",
            "link_url": "https://example.com/detail",
            "announcement_type": "notice",
            "display_position": "home",
            "style_config": {},
            "sort_order": 0,
            "start_at": datetime.now(),
            "end_at": None,
        }
    )

    assert response.content == '<img><p>正文</p>'
    assert response.image_url is None
    assert response.link_url == "https://example.com/detail"
