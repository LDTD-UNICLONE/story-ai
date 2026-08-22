from io import BytesIO
from types import SimpleNamespace

import pytest

from app.api.v1.endpoints import uploads as upload_endpoint
from app.core.exceptions import AppException
from app.schemas.upload import UploadFileOut
from app.services import uploads as upload_service
from app.services.uploads import (
    detect_content_type,
    detect_file_type,
    matches_media_signature,
    upload_story_file,
)


def test_detect_content_type_falls_back_to_media_extension_for_generic_mime() -> None:
    assert detect_content_type("clip.mp4", "application/octet-stream") == "video/mp4"
    assert detect_content_type("clip.mov", "") == "video/quicktime"
    assert detect_content_type("voice.mp3", "application/octet-stream") == "audio/mpeg"
    assert detect_content_type("voice.wav", "") == "audio/wav"
    assert detect_content_type("image.heic", "") == "image/heic"


def test_detect_content_type_normalizes_application_media_mime() -> None:
    assert detect_content_type("clip.mp4", "application/mp4") == "video/mp4"
    assert detect_content_type("clip.mkv", "application/x-matroska") == "video/x-matroska"


def test_detect_content_type_normalizes_common_browser_media_aliases() -> None:
    assert detect_content_type("portrait.jpg", "image/jpg") == "image/jpeg"
    assert detect_content_type("portrait.png", "image/x-png") == "image/png"
    assert detect_content_type("portrait.png", "image/apng") == "image/png"
    assert detect_content_type("voice.m4a", "audio/x-m4a") == "audio/mp4"


@pytest.mark.parametrize("declared_type", ["image/png", "image/x-png", "image/apng"])
def test_real_png_signature_accepts_browser_mime_variants(declared_type: str) -> None:
    content_type = detect_content_type("portrait.png", declared_type)
    png = BytesIO(b"\x89PNG\r\n\x1a\n" + b"payload")

    assert content_type == "image/png"
    assert matches_media_signature(png, content_type) is True


def test_detect_file_type_groups_media_and_regular_files() -> None:
    assert detect_file_type("image/png") == "image"
    assert detect_file_type("video/mp4") == "video"
    assert detect_file_type("application/mp4") == "video"
    assert detect_file_type("audio/mpeg") == "audio"
    assert detect_file_type("application/pdf") == "file"


def test_media_signature_must_match_declared_type() -> None:
    png = BytesIO(b"\x89PNG\r\n\x1a\n" + b"payload")
    assert matches_media_signature(png, "image/png") is True
    assert matches_media_signature(png, "image/jpeg") is False


def test_media_signature_rejects_unsupported_image_type() -> None:
    svg = BytesIO(b"<svg xmlns='http://www.w3.org/2000/svg'></svg>")
    assert matches_media_signature(svg, "image/svg+xml") is False


@pytest.mark.parametrize(
    ("content", "content_type"),
    [
        (b"RIFF\x00\x00\x00\x00WAVE", "audio/wav"),
        (b"ID3\x04\x00\x00", "audio/mpeg"),
        (b"\xff\xf1\x50\x80", "audio/aac"),
        (b"OggS\x00\x02", "audio/ogg"),
        (b"fLaC\x00\x00", "audio/flac"),
    ],
)
def test_media_signature_accepts_supported_audio(content: bytes, content_type: str) -> None:
    assert matches_media_signature(BytesIO(content), content_type) is True


def test_media_signature_accepts_webm_audio_container() -> None:
    assert matches_media_signature(BytesIO(b"\x1aE\xdf\xa3" + b"payload"), "audio/webm") is True


@pytest.mark.asyncio
async def test_generic_upload_endpoint_accepts_regular_files(monkeypatch) -> None:
    captured = {}

    async def fake_upload(file, category="", *, media_only=False):
        captured.update(category=category, media_only=media_only)
        return UploadFileOut(
            url="https://oss.example.com/document.pdf",
            object_key="story/document.pdf",
            filename="document.pdf",
            content_type="application/pdf",
            size=8,
            file_type="file",
        )

    monkeypatch.setattr(upload_endpoint, "upload_story_file", fake_upload)

    response = await upload_endpoint.upload_file(
        file=SimpleNamespace(filename="document.pdf"),
        category="documents",
        current_user=SimpleNamespace(id="user-1"),
    )

    assert response.data["file_type"] == "file"
    assert captured == {
        "category": "user-uploads/user-1/documents",
        "media_only": False,
    }


@pytest.mark.asyncio
async def test_media_only_upload_rejects_spoofed_file_before_oss_upload() -> None:
    file = SimpleNamespace(
        filename="reference.png",
        content_type="image/png",
        file=BytesIO(b"<script>alert(1)</script>"),
    )

    with pytest.raises(AppException, match="媒体类型不匹配"):
        await upload_story_file(file, media_only=True)


@pytest.mark.asyncio
async def test_general_upload_still_rejects_spoofed_media_before_oss_upload() -> None:
    file = SimpleNamespace(
        filename="reference.png",
        content_type="image/png",
        file=BytesIO(b"<script>alert(1)</script>"),
    )

    with pytest.raises(AppException, match="媒体类型不匹配"):
        await upload_story_file(file)


@pytest.mark.asyncio
async def test_upload_uses_detected_image_type_when_browser_mime_is_wrong(
    monkeypatch,
) -> None:
    captured = {}

    class FakeOssClient:
        def upload_fileobj(self, file_obj, filename, directory, content_type):
            captured.update(filename=filename, content_type=content_type)
            return "https://oss.example.com/image", "story/image"

    monkeypatch.setattr(upload_service, "OssClient", FakeOssClient)
    file = SimpleNamespace(
        filename="portrait.png",
        content_type="image/png",
        file=BytesIO(b"\xff\xd8\xff\xe0" + b"payload"),
    )

    result = await upload_story_file(file)

    assert result.file_type == "image"
    assert result.content_type == "image/jpeg"
    assert captured["content_type"] == "image/jpeg"
