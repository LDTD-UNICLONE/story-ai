from io import BytesIO
from types import SimpleNamespace

import pytest

from app.core.exceptions import AppException
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


@pytest.mark.asyncio
async def test_media_only_upload_rejects_spoofed_file_before_oss_upload() -> None:
    file = SimpleNamespace(
        filename="reference.png",
        content_type="image/png",
        file=BytesIO(b"<script>alert(1)</script>"),
    )

    with pytest.raises(AppException, match="媒体类型不匹配"):
        await upload_story_file(file, media_only=True)
