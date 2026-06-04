from collections.abc import Iterator
from typing import Optional, Tuple
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.integrations.oss import OssClient
from app.models.work import UserWorkMedia, UserWorkUpload


async def stream_work_media(media: UserWorkMedia, request: Request) -> Response:
    return await _stream_private_object(
        object_key=media.object_key,
        filename=media.filename,
        content_type=media.content_type,
        size=media.size,
        request=request,
    )


async def stream_work_media_thumbnail(media: UserWorkMedia, request: Request) -> Response:
    object_key = media.thumbnail_object_key or media.object_key
    return await _stream_private_object(
        object_key=object_key,
        filename=media.filename,
        content_type="image/jpeg" if media.thumbnail_object_key else media.content_type,
        size=0 if media.thumbnail_object_key else media.size,
        request=request,
    )


async def stream_work_upload_preview(upload: UserWorkUpload, request: Request) -> Response:
    return await _stream_private_object(
        object_key=upload.object_key,
        filename=upload.filename,
        content_type=upload.content_type,
        size=upload.size,
        request=request,
    )


async def _stream_private_object(
    *,
    object_key: str,
    filename: str,
    content_type: str,
    size: int,
    request: Request,
) -> Response:
    range_header = request.headers.get("range")
    if range_header and size > 0 and _parse_range_header(range_header, size) is None:
        return Response(
            status_code=416,
            headers={
                "Content-Range": f"bytes */{size}",
                "Accept-Ranges": "bytes",
                "Access-Control-Expose-Headers": "Content-Range, Accept-Ranges, Content-Length, Content-Type",
            },
        )
    byte_range = _parse_range_header(range_header, size)
    oss_client = OssClient()
    status_code = 200
    headers = _private_media_headers(filename)

    if byte_range:
        start, end = byte_range
        oss_object = await run_in_threadpool(oss_client.get_object_range, object_key, start, end)
        status_code = 206
        content_length = end - start + 1
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        headers["Content-Length"] = str(content_length)
        headers["Accept-Ranges"] = "bytes"
    else:
        oss_object = await run_in_threadpool(oss_client.get_object, object_key)
        if size > 0:
            headers["Content-Length"] = str(size)
        headers["Accept-Ranges"] = "bytes"

    return StreamingResponse(
        _iter_oss_object(oss_object),
        status_code=status_code,
        media_type=content_type,
        headers=headers,
    )


def _private_media_headers(filename: str) -> dict:
    return {
        "Cache-Control": "private, no-store",
        "Content-Disposition": f"inline; filename*=UTF-8''{quote(filename)}",
        "Pragma": "no-cache",
        "X-Content-Type-Options": "nosniff",
        "X-Robots-Tag": "noindex, nofollow",
        "Access-Control-Expose-Headers": "Content-Range, Accept-Ranges, Content-Length, Content-Type",
    }


def _parse_range_header(value: Optional[str], size: int) -> Optional[Tuple[int, int]]:
    if not value or size <= 0:
        return None
    if not value.startswith("bytes="):
        return None
    range_value = value.removeprefix("bytes=").split(",", 1)[0].strip()
    if "-" not in range_value:
        return None
    start_text, _, end_text = range_value.partition("-")
    try:
        if start_text:
            start = int(start_text)
            end = int(end_text) if end_text else size - 1
        else:
            suffix_length = int(end_text)
            if suffix_length <= 0:
                return None
            start = max(size - suffix_length, 0)
            end = size - 1
    except ValueError:
        return None
    if start < 0 or start >= size:
        return None
    end = min(end, size - 1)
    if end < start:
        return None
    return start, end


def _iter_oss_object(oss_object) -> Iterator[bytes]:
    try:
        while True:
            chunk = oss_object.read(1024 * 64)
            if not chunk:
                break
            yield chunk
    finally:
        close = getattr(oss_object, "close", None)
        if close:
            close()
