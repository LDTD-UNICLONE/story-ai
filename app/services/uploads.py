import mimetypes
from pathlib import PurePosixPath

from fastapi import UploadFile
from starlette.concurrency import run_in_threadpool

from app.core.exceptions import AppException
from app.core.config import settings
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


def detect_content_type(filename: str, content_type: str = "") -> str:
    normalized = (content_type or "").strip().lower()
    guessed_type = _guess_content_type(filename)
    if normalized not in GENERIC_CONTENT_TYPES:
        if guessed_type and normalized.startswith("application/") and _is_media_content_type(guessed_type):
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


def _guess_content_type(filename: str) -> str:
    suffix = PurePosixPath(filename or "").suffix.lower()
    if suffix in EXTENSION_CONTENT_TYPES:
        return EXTENSION_CONTENT_TYPES[suffix]
    guessed_type, _ = mimetypes.guess_type(filename or "")
    return (guessed_type or "").lower()


def _is_media_content_type(content_type: str) -> bool:
    return content_type.startswith(("image/", "video/", "audio/"))


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


async def upload_story_file(file: UploadFile, category: str = "") -> UploadFileOut:
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
        raise AppException(f"上传文件不能超过 {settings.max_upload_size_mb}MB", code=41300, status_code=413)

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
        file_type=detect_file_type(content_type),
    )
