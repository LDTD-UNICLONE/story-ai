import asyncio
import base64
import logging
import mimetypes
import re
import time
from io import BytesIO
from pathlib import PurePosixPath
from tempfile import SpooledTemporaryFile
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlparse

import httpx
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.exceptions import AppException
from app.core.logging import log_extra
from app.integrations.oss import OssClient
from app.services.model_runner import ModelRunResult
from app.services.uploads import build_story_directory, detect_content_type


logger = logging.getLogger(__name__)


async def persist_generated_media_to_oss(
    generation_type: str,
    model_result: ModelRunResult,
) -> ModelRunResult:
    if generation_type not in {"image", "video"}:
        return model_result

    source_urls = _collect_result_media_urls(model_result)
    sidecar_image_urls = _collect_sidecar_image_urls(model_result, source_urls)
    logger.info(
        "Generated media persistence started",
        extra=log_extra(
            event="generated_media_persist_started",
            generation_type=generation_type,
            source_url_count=len(source_urls),
            sidecar_image_url_count=len(sidecar_image_urls),
        ),
    )
    if not source_urls:
        oss_url = await _try_upload_base64_content_to_oss(model_result.content, generation_type)
        if not oss_url:
            return await _persist_sidecar_images(model_result, sidecar_image_urls)
        extra = {
            **model_result.extra,
            "oss_media_urls": [oss_url],
            "display_media_urls": [oss_url],
        }
        return await _persist_sidecar_images(ModelRunResult(content=oss_url, extra=extra), sidecar_image_urls)

    uploaded_urls = await _upload_urls_to_oss(source_urls, generation_type)
    if not uploaded_urls:
        logger.warning(
            "Generated media persistence skipped: no uploaded URLs",
            extra=log_extra(
                event="generated_media_persist_skipped",
                generation_type=generation_type,
                source_url_count=len(source_urls),
            ),
        )
        return await _persist_sidecar_images(model_result, sidecar_image_urls)

    url_mapping = dict(zip(source_urls, uploaded_urls))
    extra = _replace_urls_in_data(model_result.extra, url_mapping)
    extra = {
        **extra,
        "oss_media_urls": uploaded_urls,
        "display_media_urls": uploaded_urls,
    }
    content = _replace_result_media_urls(model_result.content, source_urls, uploaded_urls)
    logger.info(
        "Generated media persistence completed",
        extra=log_extra(
            event="generated_media_persist_completed",
            generation_type=generation_type,
            source_url_count=len(source_urls),
            uploaded_url_count=len(uploaded_urls),
        ),
    )
    return await _persist_sidecar_images(ModelRunResult(content=content, extra=extra), sidecar_image_urls)


def _collect_result_media_urls(model_result: ModelRunResult) -> List[str]:
    urls: List[str] = []
    seen: Set[str] = set()
    for value in _split_content_urls(model_result.content):
        _append_url(urls, seen, value)
    return urls


async def _persist_sidecar_images(
    model_result: ModelRunResult,
    source_urls: List[str],
) -> ModelRunResult:
    if not source_urls:
        return model_result

    uploaded_urls = await _upload_urls_to_oss(source_urls, "image")
    if not uploaded_urls:
        return model_result

    url_mapping = dict(zip(source_urls, uploaded_urls))
    extra = _replace_urls_in_data(model_result.extra, url_mapping)
    extra = {
        **extra,
        "oss_last_frame_urls": uploaded_urls,
        "display_last_frame_urls": uploaded_urls,
    }
    return ModelRunResult(content=model_result.content, extra=extra)


def _replace_urls_in_data(value: Any, url_mapping: Dict[str, str]) -> Any:
    if not url_mapping:
        return value
    if isinstance(value, str):
        replaced = value
        for source_url, oss_url in url_mapping.items():
            replaced = replaced.replace(source_url, oss_url)
        return replaced
    if isinstance(value, dict):
        return {key: _replace_urls_in_data(item, url_mapping) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_urls_in_data(item, url_mapping) for item in value]
    return value


def _collect_sidecar_image_urls(model_result: ModelRunResult, excluded_urls: List[str]) -> List[str]:
    urls: List[str] = []
    seen: Set[str] = set(excluded_urls)
    for value in _find_sidecar_image_values(model_result.extra):
        _append_url(urls, seen, value)
    return urls


def _find_sidecar_image_values(value: Any) -> List[Any]:
    values: List[Any] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if _is_last_frame_key(str(key)):
                values.append(item)
            values.extend(_find_sidecar_image_values(item))
    elif isinstance(value, list):
        for item in value:
            values.extend(_find_sidecar_image_values(item))
    return values


def _is_last_frame_key(key: str) -> bool:
    normalized = re.sub(r"(?<!^)(?=[A-Z])", "_", key).replace("-", "_").lower()
    return normalized in {
        "last_frame",
        "last_frame_url",
        "last_frame_urls",
        "tail_frame",
        "tail_frame_url",
        "tail_frame_urls",
        "end_frame",
        "end_frame_url",
        "end_frame_urls",
        "final_frame",
        "final_frame_url",
        "final_frame_urls",
    }


def _split_content_urls(content: str) -> List[str]:
    if not content:
        return []
    urls = _extract_remote_urls_from_text(content)
    if urls:
        return urls
    return [item.strip() for item in content.split(",") if item.strip()]


def _replace_result_media_urls(content: str, source_urls: List[str], uploaded_urls: List[str]) -> str:
    url_mapping = dict(zip(source_urls, uploaded_urls))
    if not url_mapping:
        return content
    if _is_media_url_list_content(content, source_urls):
        return ",".join(_dedupe_preserve_order(uploaded_urls))
    replaced_content = content or ""
    for source_url, oss_url in url_mapping.items():
        replaced_content = replaced_content.replace(source_url, oss_url)
    return replaced_content or ",".join(_dedupe_preserve_order(uploaded_urls))


def _extract_remote_urls_from_text(content: str) -> List[str]:
    urls = []
    seen: Set[str] = set()
    for match in re.findall(r"https?://[^\s,，\]\[\"'<>）)]+", content or ""):
        url = match.rstrip("。；;、，,.)）]")
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _is_media_url_list_content(content: str, source_urls: List[str]) -> bool:
    if not content:
        return True
    normalized = content.strip()
    if not normalized:
        return True
    urls = _extract_remote_urls_from_text(normalized)
    if not urls:
        return False
    text_without_urls = normalized
    for url in urls:
        text_without_urls = text_without_urls.replace(url, "")
    text_without_urls = re.sub(r"[\s,，;；、|/\\\[\]\(\)（）\"'。]+", "", text_without_urls)
    return not text_without_urls and set(urls).issubset(set(source_urls))


def _dedupe_preserve_order(values: List[str]) -> List[str]:
    items: List[str] = []
    seen: Set[str] = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            items.append(value)
    return items


def _append_url(urls: List[str], seen: Set[str], value: Any) -> None:
    url = _extract_url(value)
    if not url or url in seen:
        return
    seen.add(url)
    urls.append(url)


def _extract_url(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value if _is_remote_url(value) else None
    if isinstance(value, dict):
        for key in ("url", "image_url", "video_url", "file_url", "oss_url"):
            nested = value.get(key)
            if isinstance(nested, str) and _is_remote_url(nested):
                return nested
            if isinstance(nested, dict):
                url = _extract_url(nested)
                if url:
                    return url
    return None


def _is_remote_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


async def _upload_urls_to_oss(source_urls: List[str], generation_type: str) -> List[str]:
    timeout = httpx.Timeout(
        settings.generated_media_connect_timeout_seconds,
        read=settings.generated_media_read_timeout_seconds,
    )
    limits = httpx.Limits(
        max_connections=max(1, settings.generated_media_transfer_concurrency),
        max_keepalive_connections=max(1, settings.generated_media_transfer_concurrency),
    )
    semaphore = asyncio.Semaphore(max(1, settings.generated_media_transfer_concurrency))
    async with httpx.AsyncClient(timeout=timeout, limits=limits, follow_redirects=True) as client:
        tasks = [
            _upload_url_to_oss_with_limit(semaphore, client, source_url, generation_type)
            for source_url in source_urls
        ]
        return list(await asyncio.gather(*tasks))


async def _upload_url_to_oss_with_limit(
    semaphore: asyncio.Semaphore,
    client: httpx.AsyncClient,
    source_url: str,
    generation_type: str,
) -> str:
    async with semaphore:
        return await _upload_url_to_oss(client, source_url, generation_type)


async def _upload_url_to_oss(
    client: httpx.AsyncClient,
    source_url: str,
    generation_type: str,
) -> str:
    started_at = time.monotonic()
    content_type = ""
    fileobj = SpooledTemporaryFile(max_size=8 * 1024 * 1024)
    try:
        logger.info(
            "Generated media download started",
            extra=log_extra(
                event="generated_media_download_started",
                generation_type=generation_type,
                source_url=source_url,
            ),
        )
        async with client.stream("GET", source_url) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            content_length = _parse_content_length(response.headers.get("content-length"))
            max_size = settings.generated_media_download_max_size_mb * 1024 * 1024
            if content_length and content_length > max_size:
                raise AppException(
                    f"生成媒体不能超过 {settings.generated_media_download_max_size_mb}MB",
                    code=41301,
                    status_code=413,
                )

            downloaded_size = 0
            async for chunk in response.aiter_bytes():
                downloaded_size += len(chunk)
                if downloaded_size > max_size:
                    raise AppException(
                        f"生成媒体不能超过 {settings.generated_media_download_max_size_mb}MB",
                        code=41301,
                        status_code=413,
                    )
                fileobj.write(chunk)
    except httpx.HTTPError as exc:
        fileobj.close()
        logger.warning(
            "Generated media download failed",
            extra=log_extra(
                event="generated_media_download_failed",
                generation_type=generation_type,
                source_url=source_url,
                reason=str(exc),
            ),
        )
        raise AppException("生成媒体下载失败，无法转存 OSS", code=50230, status_code=502) from exc

    fileobj.seek(0)
    content_type = detect_content_type(_filename_from_url(source_url), content_type)
    filename = _filename_from_url(source_url, content_type)
    category = "image" if generation_type == "image" else "video"
    logger.info(
        "Generated media download completed",
        extra=log_extra(
            event="generated_media_download_completed",
            generation_type=generation_type,
            source_url=source_url,
            filename=filename,
            content_type=content_type,
            elapsed_ms=round((time.monotonic() - started_at) * 1000, 2),
        ),
    )

    oss_client = OssClient()
    try:
        logger.info(
            "Generated media OSS upload started",
            extra=log_extra(
                event="generated_media_oss_upload_started",
                generation_type=generation_type,
                filename=filename,
                directory=build_story_directory(category),
                content_type=content_type,
                timeout_seconds=settings.generated_media_upload_timeout_seconds,
            ),
        )
        upload_started_at = time.monotonic()
        url, _ = await asyncio.wait_for(
            run_in_threadpool(
                oss_client.upload_fileobj,
                fileobj,
                filename,
                build_story_directory(category),
                content_type,
            ),
            timeout=settings.generated_media_upload_timeout_seconds,
        )
        logger.info(
            "Generated media OSS upload completed",
            extra=log_extra(
                event="generated_media_oss_upload_completed",
                generation_type=generation_type,
                filename=filename,
                oss_url=url,
                elapsed_ms=round((time.monotonic() - upload_started_at) * 1000, 2),
            ),
        )
        return url
    except asyncio.TimeoutError as exc:
        logger.error(
            "Generated media OSS upload timed out",
            extra=log_extra(
                event="generated_media_oss_upload_timeout",
                generation_type=generation_type,
                filename=filename,
                timeout_seconds=settings.generated_media_upload_timeout_seconds,
            ),
        )
        raise AppException("生成媒体转存 OSS 超时，请稍后重试", code=50232, status_code=502) from exc
    except Exception as exc:
        logger.error(
            "Generated media OSS upload failed",
            extra=log_extra(
                event="generated_media_oss_upload_failed",
                generation_type=generation_type,
                filename=filename,
                reason=str(exc),
            ),
        )
        raise
    finally:
        fileobj.close()


def _parse_content_length(value: Optional[str]) -> int:
    try:
        return int(value or 0)
    except ValueError:
        return 0


async def _try_upload_base64_content_to_oss(content: str, generation_type: str) -> Optional[str]:
    if generation_type != "image" or not content:
        return None

    content_type = "image/png"
    raw_content = content.strip()
    if raw_content.startswith("data:"):
        header, _, raw_content = raw_content.partition(",")
        if ";base64" not in header or not raw_content:
            return None
        content_type = header.removeprefix("data:").split(";")[0] or content_type

    try:
        data = base64.b64decode(raw_content, validate=True)
    except Exception:
        return None
    if not data:
        return None

    filename = _filename_from_url("", content_type)
    oss_client = OssClient()
    try:
        url, _ = await asyncio.wait_for(
            run_in_threadpool(
                oss_client.upload_fileobj,
                BytesIO(data),
                filename,
                build_story_directory("image"),
                content_type,
            ),
            timeout=settings.generated_media_upload_timeout_seconds,
        )
    except asyncio.TimeoutError as exc:
        raise AppException("生成媒体转存 OSS 超时，请稍后重试", code=50232, status_code=502) from exc
    return url


def _filename_from_url(source_url: str, content_type: str = "") -> str:
    path = urlparse(source_url).path
    filename = PurePosixPath(path).name
    if filename and PurePosixPath(filename).suffix:
        return filename

    extension = mimetypes.guess_extension((content_type or "").split(";")[0].strip()) or ""
    return f"generated{extension}"
