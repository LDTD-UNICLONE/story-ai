from pathlib import PurePosixPath
from typing import BinaryIO, Optional, Tuple
from urllib.parse import quote
from uuid import uuid4

import oss2

from app.core.config import settings
from app.core.exceptions import AppException


class OssClient:
    def __init__(self) -> None:
        if not all(
            [
                settings.oss_endpoint,
                settings.oss_bucket_name,
                settings.oss_access_key_id,
                settings.oss_access_key_secret,
            ]
        ):
            raise AppException("OSS 配置不完整", code=50010, status_code=500)

        auth = oss2.Auth(settings.oss_access_key_id, settings.oss_access_key_secret)
        self.bucket = oss2.Bucket(
            auth,
            settings.oss_endpoint,
            settings.oss_bucket_name,
            connect_timeout=settings.generated_media_connect_timeout_seconds,
        )

    def upload_fileobj(
        self,
        fileobj: BinaryIO,
        filename: str,
        directory: str = "uploads",
        content_type: Optional[str] = None,
    ) -> Tuple[str, str]:
        suffix = PurePosixPath(filename).suffix
        object_key = str(PurePosixPath(directory) / f"{uuid4().hex}{suffix}")
        headers = {"Content-Type": content_type} if content_type else None
        try:
            result = self.bucket.put_object(object_key, fileobj, headers=headers)
        except Exception as exc:
            raise AppException(
                f"OSS 上传失败：{_oss_exception_message(exc)}", code=50011, status_code=500
            ) from exc
        if result.status >= 300:
            request_id = getattr(result, "request_id", "") or "-"
            raise AppException(
                f"OSS 上传失败：status={result.status}, request_id={request_id}",
                code=50011,
                status_code=500,
            )
        return self.public_url(object_key), object_key

    def public_url(self, object_key: str) -> str:
        if settings.oss_public_base_url:
            return f"{settings.oss_public_base_url.rstrip('/')}/{object_key.lstrip('/')}"
        return f"https://{settings.oss_bucket_name}.{settings.oss_endpoint.removeprefix('https://')}/{object_key}"

    def signed_download_url(self, object_key: str, filename: str) -> str:
        params = {
            "response-content-disposition": (
                f"inline; filename*=UTF-8''{quote(filename, safe='')}"
            )
        }
        try:
            return self.bucket.sign_url(
                "GET",
                object_key,
                max(1, settings.oss_signed_url_expires_seconds),
                params=params,
                slash_safe=True,
            )
        except Exception as exc:
            raise AppException("OSS 文件签名失败", code=50012, status_code=500) from exc

    def delete_object(self, object_key: str) -> None:
        try:
            result = self.bucket.delete_object(object_key)
        except Exception as exc:
            raise AppException(
                f"OSS 文件删除失败：{_oss_exception_message(exc)}", code=50014, status_code=500
            ) from exc
        if result.status >= 300:
            request_id = getattr(result, "request_id", "") or "-"
            raise AppException(
                f"OSS 文件删除失败：status={result.status}, request_id={request_id}",
                code=50014,
                status_code=500,
            )


def _oss_exception_message(exc: Exception) -> str:
    parts = []
    for attr in ("status", "code", "message", "request_id"):
        value = getattr(exc, attr, None)
        if value:
            parts.append(f"{attr}={value}")
    return ", ".join(parts) or str(exc) or exc.__class__.__name__
