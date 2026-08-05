import html
from typing import Literal, Optional
from urllib.parse import unquote, urlsplit

import nh3

from app.core.exceptions import AppException


_ALLOWED_TAGS = {
    "a",
    "b",
    "blockquote",
    "br",
    "code",
    "div",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "hr",
    "i",
    "img",
    "li",
    "ol",
    "p",
    "pre",
    "s",
    "span",
    "strong",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "u",
    "ul",
}
_CLEAN_CONTENT_TAGS = {
    "embed",
    "iframe",
    "math",
    "noscript",
    "object",
    "script",
    "style",
    "svg",
    "template",
}
_HTML_CLEANER = nh3.Cleaner(
    tags=_ALLOWED_TAGS,
    clean_content_tags=_CLEAN_CONTENT_TAGS,
    attributes={
        "a": {"href", "title"},
        "img": {"alt", "height", "src", "title", "width"},
        "td": {"colspan", "rowspan"},
        "th": {"colspan", "rowspan"},
    },
    url_schemes={"http", "https"},
    url_relative="deny",
    link_rel="noopener noreferrer",
    strip_comments=True,
)
_DANGEROUS_SCHEMES = ("javascript:", "vbscript:", "data:", "file:")


def sanitize_announcement_content(
    content: str,
    content_format: str,
    *,
    strict: bool = True,
) -> str:
    if content_format == "plain":
        return content

    if content_format == "markdown" and _contains_dangerous_scheme(content):
        if strict:
            raise AppException("公告内容包含不安全链接", code=40022, status_code=400)
        return ""

    cleaned = _HTML_CLEANER.clean(content)
    if strict and not cleaned.strip():
        raise AppException("公告内容清洗后不能为空", code=40022, status_code=400)
    return cleaned


def validate_announcement_url(
    value: Optional[str],
    field_name: Literal["公告图片地址", "公告跳转地址"],
) -> Optional[str]:
    if value is None:
        return None

    normalized = value.strip()
    if not normalized:
        return None
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in normalized):
        raise _unsafe_url(field_name)

    try:
        parsed = urlsplit(normalized)
        port = parsed.port
    except ValueError as exc:
        raise _unsafe_url(field_name) from exc

    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None and not 1 <= port <= 65535
    ):
        raise _unsafe_url(field_name)
    return normalized


def safe_announcement_url(value: Optional[str]) -> Optional[str]:
    try:
        return validate_announcement_url(value, "公告跳转地址")
    except AppException:
        return None


def _contains_dangerous_scheme(value: str) -> bool:
    canonical = html.unescape(value)
    for _ in range(2):
        decoded = unquote(canonical)
        if decoded == canonical:
            break
        canonical = decoded
    canonical = "".join(character for character in canonical if ord(character) > 0x20)
    canonical = canonical.casefold()
    return any(scheme in canonical for scheme in _DANGEROUS_SCHEMES)


def _unsafe_url(field_name: str) -> AppException:
    return AppException(
        f"{field_name}仅支持不含账号信息的 HTTP/HTTPS 绝对地址",
        code=40021,
        status_code=400,
    )
