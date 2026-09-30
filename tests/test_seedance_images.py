from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from app.core.exceptions import AppException
from app.integrations import apimart
from app.schemas.conversation import ConversationSendMessageRequest
from app.services.seedance_images import image_fingerprint
from app.tasks.seedance_images import usable_asset


def test_fingerprint_uses_content_not_filename_and_restores_stream():
    data = b"\x89PNG\r\n\x1a\n" + b"same-file"
    first = SimpleNamespace(filename="a.png", file=BytesIO(data))
    renamed = SimpleNamespace(filename="b.png", file=BytesIO(data))
    changed = SimpleNamespace(filename="a.png", file=BytesIO(data + b"changed"))
    assert image_fingerprint(first) == image_fingerprint(renamed)
    assert image_fingerprint(first) != image_fingerprint(changed)
    assert first.file.tell() == renamed.file.tell() == changed.file.tell() == 0


@pytest.mark.parametrize("data", [b"", b"not-an-image"])
def test_fingerprint_rejects_invalid_files(data):
    with pytest.raises(AppException):
        image_fingerprint(SimpleNamespace(file=BytesIO(data)))


@pytest.mark.parametrize("status", ["completed", "failed"])
def test_only_explicitly_usable_asset_passes_even_for_failed_task(status):
    assert (
        usable_asset(
            {
                "status": status,
                "result": {
                    "usable_assets": [{"asset_url": "asset://one", "status": "Active"}],
                },
            }
        )
        == "asset://one"
    )


@pytest.mark.parametrize(
    "result",
    [
        {},
        {"asset_url": "https://example.com/image.png"},
        {"assets": [{"asset_url": "asset://one", "status": "Failed"}]},
        {"usable_assets": [{"asset_url": "asset://", "status": "Active"}]},
        {"assets": [{"asset_url": "asset://one", "status": "Active"}, {"status": "Failed"}]},
    ],
)
def test_completed_is_not_sufficient_for_image_approval(result):
    assert usable_asset({"status": "completed", "result": result}) is None


def test_single_asset_legacy_result_is_supported():
    assert (
        usable_asset({"status": "completed", "result": {"asset_url": "asset://one"}})
        == "asset://one"
    )


def test_image_reference_requires_exactly_one_source():
    image_id = str(uuid4())
    request = ConversationSendMessageRequest(
        content="@{图片1}", image_references=[{"name": "图片1", "image_id": image_id}]
    )
    assert str(request.image_references[0].image_id) == image_id
    for reference in (
        {"name": "图片1"},
        {"name": "图片1", "image_id": image_id, "url": "https://example.com/a.png"},
    ):
        with pytest.raises(ValidationError):
            ConversationSendMessageRequest(content="@{图片1}", image_references=[reference])


@pytest.mark.asyncio
async def test_private_avatar_uses_confirmed_endpoint_without_automatic_retry(monkeypatch):
    calls = []

    class Client:
        async def post(self, path, **kwargs):
            calls.append((path, kwargs))
            return httpx.Response(
                200, json={"code": 200, "data": {"id": "review-1", "status": "processing"}}
            )

    async def get_client():
        return Client()

    monkeypatch.setattr(apimart, "_get_client", get_client)
    result = await apimart.create_private_avatar("https://example.com/a.png", "image-one")
    assert result["id"] == "review-1"
    path, options = calls[0]
    assert path == "/seedance2/private-avatar"
    assert options["options"]["max_retries"] == 0
    assert options["body"] == {
        "group": {"name": "image-one"},
        "project_name": "default",
        "asset_type": "Image",
        "assets": [{"url": "https://example.com/a.png", "name": "image-one"}],
    }
