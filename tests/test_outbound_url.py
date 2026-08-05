import socket

import httpx
import pytest

from app.core.exceptions import AppException
from app.core.outbound_url import (
    ValidatedPublicUrl,
    open_safe_http_response,
    validate_public_http_url,
)
from app.integrations.comfly import _download_upload_file
from app.services import generated_media
from app.services.uploads import probe_media_url


class FakeNetworkStream:
    def __init__(self, host: str) -> None:
        self.host = host

    def get_extra_info(self, name: str):
        return (self.host, 443) if name == "server_addr" else None


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "file:///etc/passwd",
    ],
)
def test_validate_public_http_url_rejects_private_and_non_http_urls(url: str) -> None:
    with pytest.raises(AppException):
        validate_public_http_url(url)


def test_validate_public_http_url_checks_every_resolved_address(monkeypatch) -> None:
    def fake_getaddrinfo(*_args, **_kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.10", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.10", 443)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(AppException):
        validate_public_http_url("https://media.example.com/video.mp4")


def test_validate_public_http_url_requires_configured_host(monkeypatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
        ],
    )
    with pytest.raises(AppException):
        validate_public_http_url(
            "https://attacker.example/video.mp4",
            allowed_hosts={"story.example.com"},
        )


@pytest.mark.asyncio
async def test_generated_media_revalidates_redirect_target(monkeypatch) -> None:
    checked_urls = []

    def resolve(url: str, *, allowed_hosts=()) -> ValidatedPublicUrl:
        checked_urls.append(url)
        if "127.0.0.1" in url:
            raise AppException("blocked")
        return ValidatedPublicUrl(url=url, addresses=frozenset({"8.8.8.8"}))

    monkeypatch.setattr("app.core.outbound_url.resolve_public_http_url", resolve)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"Location": "http://127.0.0.1/private"},
            extensions={"network_stream": FakeNetworkStream("8.8.8.8")},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AppException):
            await generated_media._open_safe_media_response(
                client,
                "https://media.example.com/video.mp4",
            )

    assert checked_urls == [
        "https://media.example.com/video.mp4",
        "http://127.0.0.1/private",
    ]


@pytest.mark.asyncio
async def test_safe_response_rejects_dns_rebinding_peer(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.core.outbound_url.resolve_public_http_url",
        lambda url, *, allowed_hosts=(): ValidatedPublicUrl(
            url=url,
            addresses=frozenset({"8.8.8.8"}),
        ),
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"private data",
            extensions={"network_stream": FakeNetworkStream("127.0.0.1")},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AppException, match="实际连接地址"):
            await open_safe_http_response(client, "https://media.example.com/video.mp4")


@pytest.mark.asyncio
async def test_safe_response_rejects_unverifiable_peer(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.core.outbound_url.resolve_public_http_url",
        lambda url, *, allowed_hosts=(): ValidatedPublicUrl(
            url=url,
            addresses=frozenset({"8.8.8.8"}),
        ),
    )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"data"))
    ) as client:
        with pytest.raises(AppException, match="无法验证"):
            await open_safe_http_response(client, "https://media.example.com/video.mp4")


@pytest.mark.asyncio
async def test_image_edit_download_rejects_private_network_url() -> None:
    with pytest.raises(AppException):
        await _download_upload_file("http://127.0.0.1/private.png", "image.png")


@pytest.mark.asyncio
async def test_remote_media_probe_rejects_private_network_url() -> None:
    with pytest.raises(AppException):
        await probe_media_url("http://127.0.0.1/private.mp4", "video")
