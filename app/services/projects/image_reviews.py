"""Register owned project images with the shared Seedance review cache on demand."""

from pathlib import PurePosixPath
from tempfile import SpooledTemporaryFile
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import AppException
from app.core.outbound_url import open_safe_http_response, trusted_oss_hosts
from app.schemas.upload import UploadFileOut
from app.services.seedance_images import upload_seedance_image
from app.services.uploads import detect_image_content_type


async def register_stored_image(db: AsyncSession, user_id: UUID, url: str):
    # Download only a verified owned image, with the same SSRF/redirect protection
    # used for other server-side media reads. Hash actual bytes, never the URL.
    max_size = min(30 * 1024 * 1024 - 1, settings.max_upload_size_mb * 1024 * 1024)
    file = UploadFile(
        file=SpooledTemporaryFile(max_size=1024 * 1024),
        size=0,
        filename=PurePosixPath(urlsplit(url).path).name or "generated-image",
    )
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(
                settings.generated_media_connect_timeout_seconds,
                read=settings.generated_media_read_timeout_seconds,
            ),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await open_safe_http_response(client, url, allowed_hosts=trusted_oss_hosts())
            try:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    if file.size + len(chunk) > max_size:
                        raise AppException("审核图片超过上传大小限制", code=41300, status_code=413)
                    await file.write(chunk)
            finally:
                await response.aclose()
        await file.seek(0)
        source_upload = UploadFileOut(
            url=url,
            object_key="",
            filename=file.filename,
            content_type=detect_image_content_type(file.file) or "application/octet-stream",
            size=file.size,
            file_type="image",
        )
        return await upload_seedance_image(db, user_id, file, source_upload=source_upload)
    except httpx.HTTPError as exc:
        raise AppException("项目图片读取失败，请稍后重试", code=50231, status_code=502) from exc
    finally:
        await file.close()
