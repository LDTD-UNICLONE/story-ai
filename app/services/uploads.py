import json
import mimetypes
import shutil
import subprocess
import tempfile
from pathlib import PurePosixPath
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx
from fastapi import UploadFile
from starlette.concurrency import run_in_threadpool

from app.core.exceptions import AppException
from app.core.config import settings
from app.core.outbound_url import open_safe_http_response, trusted_oss_hosts
from app.integrations.oss import OssClient
from app.schemas.upload import UploadFileOut


GENERIC_CONTENT_TYPES = {
    "",
    "application/octet-stream",
    "binary/octet-stream",
    "application/x-download",
    "application/force-download",
}

EXTENSION_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".m4v": "video/x-m4v",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    ".mpeg": "video/mpeg",
    ".mpg": "video/mpeg",
    ".3gp": "video/3gpp",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".flac": "audio/flac",
}

VIDEO_CONTENT_TYPE_ALIASES = {
    "application/mp4",
    "application/x-matroska",
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
}

AUDIO_CONTENT_TYPE_ALIASES = {
    "application/ogg",
}
MEDIA_PROBE_TIMEOUT_SECONDS = 15


def detect_content_type(filename: str, content_type: str = "") -> str:
    normalized = (content_type or "").strip().lower()
    guessed_type = _guess_content_type(filename)
    if normalized not in GENERIC_CONTENT_TYPES:
        if (
            guessed_type and normalized.startswith("application/") and _is_media_content_type(guessed_type)
        ):
            return guessed_type
        return normalized
    return guessed_type or "application/octet-stream"


def detect_file_type(content_type: str) -> str:
    content_type = (content_type or "").strip().lower()
    if content_type.startswith("image/"):
        return "image"
    if content_type.startswith("video/"):
        return "video"
    if content_type in VIDEO_CONTENT_TYPE_ALIASES:
        return "video"
    if content_type.startswith("audio/"):
        return "audio"
    if content_type in AUDIO_CONTENT_TYPE_ALIASES:
        return "audio"
    return "file"


def matches_media_signature(file_obj: Any, content_type: str) -> bool:
    current_pos = file_obj.tell()
    try:
        file_obj.seek(0)
        header = file_obj.read(32)
    finally:
        file_obj.seek(current_pos)

    normalized = (content_type or "").strip().lower()
    if normalized == "image/jpeg":
        return header.startswith(b"\xff\xd8\xff")
    if normalized == "image/png":
        return header.startswith(b"\x89PNG\r\n\x1a\n")
    if normalized == "image/webp":
        return header.startswith(b"RIFF") and header[8:12] == b"WEBP"
    if normalized == "image/gif":
        return header.startswith((b"GIF87a", b"GIF89a"))
    if normalized == "image/bmp":
        return header.startswith(b"BM")
    if normalized == "image/tiff":
        return header.startswith((b"II*\x00", b"MM\x00*"))
    if normalized in {"image/heic", "image/heif"}:
        return header[4:8] == b"ftyp" and header[8:12] in {
            b"heic",
            b"heix",
            b"hevc",
            b"hevx",
            b"mif1",
            b"msf1",
        }
    if normalized in {
        "video/mp4",
        "video/quicktime",
        "video/x-m4v",
        "video/3gpp",
        "application/mp4",
        "audio/mp4",
    }:
        return header[4:8] == b"ftyp"
    if normalized in {"video/webm", "video/x-matroska", "application/x-matroska"}:
        return header.startswith(b"\x1aE\xdf\xa3")
    if normalized == "video/x-msvideo":
        return header.startswith(b"RIFF") and header[8:12] == b"AVI "
    if normalized == "video/mpeg":
        return header.startswith((b"\x00\x00\x01\xba", b"\x00\x00\x01\xb3"))
    if normalized == "audio/wav":
        return header.startswith(b"RIFF") and header[8:12] == b"WAVE"
    if normalized == "audio/mpeg":
        return header.startswith(b"ID3") or (
            len(header) >= 2 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0
        )
    if normalized == "audio/aac":
        return len(header) >= 2 and header[0] == 0xFF and header[1] & 0xF6 == 0xF0
    if normalized in {"audio/ogg", "application/ogg"}:
        return header.startswith(b"OggS")
    if normalized == "audio/flac":
        return header.startswith(b"fLaC")
    return False


def _guess_content_type(filename: str) -> str:
    suffix = PurePosixPath(filename or "").suffix.lower()
    if suffix in EXTENSION_CONTENT_TYPES:
        return EXTENSION_CONTENT_TYPES[suffix]
    guessed_type, _ = mimetypes.guess_type(filename or "")
    return (guessed_type or "").lower()


def _is_media_content_type(content_type: str) -> bool:
    return content_type.startswith(("image/", "video/", "audio/"))


def extract_media_info(
    file_obj: Any, file_type: str, filename: str = ""
) -> Optional[Dict[str, Any]]:
    if file_type not in {"video", "audio"}:
        return None

    current_pos = file_obj.tell()
    suffix = PurePosixPath(filename or "").suffix
    try:
        file_obj.seek(0)
        with tempfile.NamedTemporaryFile(suffix=suffix) as temp_file:
            shutil.copyfileobj(file_obj, temp_file)
            temp_file.flush()
            return _probe_media_file(temp_file.name, file_type)
    finally:
        file_obj.seek(current_pos)


async def probe_media_url(url: str, file_type: str) -> Optional[Dict[str, Any]]:
    if file_type not in {"video", "audio"}:
        return None
    timeout = httpx.Timeout(
        settings.generated_media_connect_timeout_seconds,
        read=settings.generated_media_read_timeout_seconds,
    )
    max_size = max(1, settings.max_upload_size_mb) * 1024 * 1024
    suffix = PurePosixPath(urlparse(url).path).suffix
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        response = await open_safe_http_response(
            client,
            url,
            allowed_hosts=trusted_oss_hosts(),
        )
        try:
            response.raise_for_status()
            try:
                content_length = int(response.headers.get("content-length") or 0)
            except ValueError:
                content_length = 0
            if content_length > max_size:
                raise AppException(
                    f"参考媒体不能超过 {settings.max_upload_size_mb}MB",
                    code=41300,
                    status_code=413,
                )
            with tempfile.NamedTemporaryFile(suffix=suffix) as temp_file:
                downloaded_size = 0
                async for chunk in response.aiter_bytes():
                    downloaded_size += len(chunk)
                    if downloaded_size > max_size:
                        raise AppException(
                            f"参考媒体不能超过 {settings.max_upload_size_mb}MB",
                            code=41300,
                            status_code=413,
                        )
                    temp_file.write(chunk)
                temp_file.flush()
                return await run_in_threadpool(_probe_media_file, temp_file.name, file_type)
        except httpx.HTTPError:
            return None
        finally:
            await response.aclose()


def _probe_media_file(path: str, file_type: str) -> Optional[Dict[str, Any]]:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration,size,format_name:stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate",
                "-of",
                "json",
                path,
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=MEDIA_PROBE_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None

    if result.returncode != 0 or not result.stdout.strip():
        return None

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None

    return _normalize_media_probe(data, file_type)


def _normalize_media_probe(data: Dict[str, Any], file_type: str) -> Optional[Dict[str, Any]]:
    format_data = data.get("format") if isinstance(data.get("format"), dict) else {}
    streams = data.get("streams") if isinstance(data.get("streams"), list) else []
    video_stream = _first_stream(streams, "video")
    audio_stream = _first_stream(streams, "audio")

    media_info: Dict[str, Any] = {
        "duration_seconds": _optional_float(format_data.get("duration")),
        "format_name": _optional_str(format_data.get("format_name")),
        "size": _optional_int(format_data.get("size")),
    }
    if file_type == "video" and video_stream:
        media_info.update(
            {
                "video_codec": _optional_str(video_stream.get("codec_name")),
                "width": _optional_int(video_stream.get("width")),
                "height": _optional_int(video_stream.get("height")),
                "fps": _parse_frame_rate(video_stream.get("avg_frame_rate"))
                or _parse_frame_rate(video_stream.get("r_frame_rate")),
            }
        )
    if audio_stream:
        media_info["audio_codec"] = _optional_str(audio_stream.get("codec_name"))

    cleaned = {key: value for key, value in media_info.items() if value not in (None, "")}
    return cleaned or None


def _first_stream(streams: list[Any], codec_type: str) -> Optional[Dict[str, Any]]:
    for stream in streams:
        if isinstance(stream, dict) and stream.get("codec_type") == codec_type:
            return stream
    return None


def _optional_str(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _optional_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, 3)


def _parse_frame_rate(value: Any) -> Optional[float]:
    text = str(value or "").strip()
    if not text or text == "0/0":
        return None
    if "/" in text:
        numerator, denominator = text.split("/", 1)
        try:
            denominator_value = float(denominator)
            if denominator_value == 0:
                return None
            return round(float(numerator) / denominator_value, 3)
        except ValueError:
            return None
    return _optional_float(text)


def build_story_directory(category: str) -> str:
    root_directory = settings.oss_root_directory.strip().strip("/") or "story"
    cleaned = category.strip().strip("/")
    if not cleaned:
        return root_directory

    parts = []
    for part in cleaned.split("/"):
        safe_part = "".join(char for char in part if char.isalnum() or char in {"-", "_"})
        if safe_part:
            parts.append(safe_part)

    if not parts:
        return root_directory
    return f"{root_directory}/{'/'.join(parts)}"


async def upload_story_file(
    file: UploadFile,
    category: str = "",
    *,
    media_only: bool = False,
) -> UploadFileOut:
    filename = file.filename or "file"
    content_type = detect_content_type(filename, file.content_type or "")
    if not content_type:
        raise AppException("上传文件类型不能为空", code=40008, status_code=400)

    max_size = settings.max_upload_size_mb * 1024 * 1024
    file.file.seek(0, 2)
    size = file.file.tell()
    file.file.seek(0)
    if size <= 0:
        raise AppException("上传文件不能为空", code=40007, status_code=400)
    if size > max_size:
        raise AppException(
            f"上传文件不能超过 {settings.max_upload_size_mb}MB", code=41300, status_code=413
        )

    file_type = detect_file_type(content_type)
    if media_only and file_type not in {"image", "video", "audio"}:
        raise AppException("仅支持上传图片、视频或音频", code=40049, status_code=400)
    if media_only and not matches_media_signature(file.file, content_type):
        raise AppException("上传文件内容与媒体类型不匹配", code=40050, status_code=400)
    media_info = await run_in_threadpool(extract_media_info, file.file, file_type, filename)
    file.file.seek(0)

    oss_client = OssClient()
    url, object_key = await run_in_threadpool(
        oss_client.upload_fileobj,
        file.file,
        filename,
        build_story_directory(category),
        content_type,
    )
    return UploadFileOut(
        url=url,
        object_key=object_key,
        filename=filename,
        content_type=content_type,
        size=size,
        file_type=file_type,
        media_info=media_info,
    )
