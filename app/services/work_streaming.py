from fastapi.responses import RedirectResponse

from app.integrations.oss import OssClient
from app.models.work import UserWorkMedia, UserWorkUpload


async def stream_work_media(media: UserWorkMedia) -> RedirectResponse:
    return _redirect_to_private_object(
        object_key=media.object_key,
        filename=media.filename,
    )


async def stream_work_media_thumbnail(media: UserWorkMedia) -> RedirectResponse:
    object_key = media.thumbnail_object_key or media.object_key
    return _redirect_to_private_object(
        object_key=object_key,
        filename=media.filename,
    )


async def stream_work_upload_preview(upload: UserWorkUpload) -> RedirectResponse:
    return _redirect_to_private_object(
        object_key=upload.object_key,
        filename=upload.filename,
    )


def _redirect_to_private_object(*, object_key: str, filename: str) -> RedirectResponse:
    oss_client = OssClient()
    signed_url = oss_client.signed_download_url(object_key, filename)
    return RedirectResponse(
        signed_url,
        status_code=307,
        headers={
            "Cache-Control": "private, no-store",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "X-Robots-Tag": "noindex, nofollow",
        },
    )
