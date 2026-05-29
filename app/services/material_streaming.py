from collections.abc import Iterator
from urllib.parse import quote

from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.integrations.oss import OssClient
from app.models.material import Material


async def stream_material_image(material: Material) -> StreamingResponse:
    oss_client = OssClient()
    oss_object = await run_in_threadpool(oss_client.get_object, material.image_object_key)
    headers = {
        "Cache-Control": "no-store",
        "Content-Disposition": f"inline; filename*=UTF-8''{quote(material.filename)}",
        "Pragma": "no-cache",
        "X-Content-Type-Options": "nosniff",
        "X-Robots-Tag": "noindex, nofollow",
    }
    if material.size > 0:
        headers["Content-Length"] = str(material.size)
    return StreamingResponse(
        _iter_oss_object(oss_object),
        media_type=material.content_type,
        headers=headers,
    )


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
