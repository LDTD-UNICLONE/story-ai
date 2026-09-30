"""User-owned Seedance images, review reuse and generation input validation."""

import hashlib
from uuid import UUID

from fastapi import UploadFile
from sqlalchemy import cast, or_, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.exceptions import AppException
from app.core.public_messages import sanitize_public_message
from app.core.timezone import beijing_datetime
from app.integrations import apimart
from app.models.seedance_image import SeedanceImage
from app.schemas.upload import SeedanceImageOut, UploadFileOut
from app.services.generation.task_dispatch import enqueue_task_dispatch, dispatch_tasks_best_effort
from app.services.uploads import detect_image_content_type, upload_story_file


REVIEW_TASK = "tasks.seedance_images.review_image"
SEEDANCE_MODELS = {
    "seedance-2.0",
    "seedance-2.0-fast",
    "seedance-2.0-mini",
    "seedance-2-0",
    "seedance-2.0-face",
    "seedance-2.0-fast-face",
    "seedance-2.5",
}


def provider_scope() -> str:
    if not settings.apimart_api_key or not settings.apimart_base_url:
        raise AppException("APIMart 素材服务尚未配置", code=50021, status_code=500)
    # Neither the key nor a reusable credential is exposed in upload responses.
    return hashlib.sha256(
        f"{settings.apimart_base_url.rstrip('/')}\0{settings.apimart_api_key}".encode()
    ).hexdigest()


def is_seedance_model(model) -> bool:
    return (
        model.vendor == apimart.APIMART_VENDOR
        and model.model_type == "video"
        and model.model_id.strip().lower() in SEEDANCE_MODELS
    )


def image_fingerprint(file) -> str:
    stream = file.file
    stream.seek(0, 2)
    size = stream.tell()
    stream.seek(0)
    if size <= 0 or size >= 30 * 1024 * 1024 or size > settings.max_upload_size_mb * 1024 * 1024:
        raise AppException(
            "Seedance 参考图片不能为空且须小于 30MB，并满足平台上传大小限制", code=40016
        )
    if not detect_image_content_type(stream):
        raise AppException("Seedance 参考素材仅支持图片文件", code=40016)
    digest = hashlib.sha256()
    try:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    finally:
        stream.seek(0)
    return digest.hexdigest()


def image_out(record: SeedanceImage, *, reused: bool = False) -> SeedanceImageOut:
    compatible = (
        bool(settings.apimart_api_key and settings.apimart_base_url)
        and record.provider_scope == provider_scope()
    )
    error_message = record.error_message if compatible else "素材服务配置已变更，请重新上传图片"
    return SeedanceImageOut(
        **record.upload,
        image_id=record.id,
        review_status="processing" if record.status == "submitting" else record.status,
        can_reference=record.status == "ready" and compatible,
        can_retry=record.status in {"failed", "uncertain"} and compatible,
        retry_requires_confirmation=(
            record.status == "uncertain" and not record.provider_task_id and compatible
        ),
        review_error=sanitize_public_message(error_message) if error_message else None,
        reused=reused,
    )


async def upload_seedance_image(
    db: AsyncSession, user_id: UUID, file: UploadFile,
    *, source_upload: UploadFileOut | None = None,
) -> SeedanceImageOut:
    # source_upload is supplied only for verified, owned stored project images.
    # Those files already exist in storage and must not be uploaded a second time.
    scope = provider_scope()
    fingerprint = await run_in_threadpool(image_fingerprint, file)
    # Serialize identical uploads before OSS I/O as well as before DB insertion.
    lock_key = int.from_bytes(
        hashlib.sha256(f"{user_id}:{scope}:{fingerprint}".encode()).digest()[:8], "big", signed=True
    )
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
    record = await db.scalar(
        select(SeedanceImage).where(
            SeedanceImage.user_id == user_id,
            SeedanceImage.provider_scope == scope,
            SeedanceImage.sha256 == fingerprint,
        )
    )
    if record is not None:
        if source_upload and source_upload.url != record.upload["url"]:
            aliases = list(record.upload.get("source_urls") or [])
            if source_upload.url not in aliases:
                record.upload = {**record.upload, "source_urls": [*aliases, source_upload.url]}
        result = image_out(record, reused=True)
        await db.commit()
        return result

    uploaded = source_upload or await upload_story_file(
        file, category=f"user-uploads/{user_id}/seedance", media_only=True
    )
    record = SeedanceImage(
        user_id=user_id,
        provider_scope=scope,
        sha256=fingerprint,
        upload=uploaded.model_dump(mode="json"),
        status="pending",
        next_poll_at=beijing_datetime(),
    )
    db.add(record)
    await db.flush()
    dispatch_id = await enqueue_task_dispatch(
        db,
        task_name=REVIEW_TASK,
        args=(str(record.id),),
        queue="story_ai_default",
        message_id=record.id,
    )
    result = image_out(record)
    await db.commit()
    await dispatch_tasks_best_effort(db, [dispatch_id])
    return result


async def get_image(
    db: AsyncSession, user_id: UUID, image_id: UUID, *, lock: bool = False
) -> SeedanceImage:
    query = select(SeedanceImage).where(
        SeedanceImage.id == image_id, SeedanceImage.user_id == user_id
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    record = await db.scalar(query)
    if record is None:
        raise AppException("图片不存在", code=40420, status_code=404)
    return record


async def retry_image(
    db: AsyncSession, user_id: UUID, image_id: UUID, *, confirm_resubmit: bool = False
) -> SeedanceImageOut:
    record = await get_image(db, user_id, image_id, lock=True)
    if record.provider_scope != provider_scope():
        raise AppException("素材服务配置已变更，请重新上传图片", code=40016)
    if record.status == "uncertain" and not record.provider_task_id and not confirm_resubmit:
        raise AppException(
            "上次提交结果不明确，重新提交可能重复入库，请确认后重试", code=40999, status_code=409
        )
    if record.status in {"failed", "uncertain"}:
        resume_query = record.status == "uncertain" and bool(record.provider_task_id)
        record.status = "processing" if resume_query else "pending"
        if not resume_query:
            record.provider_task_id = None
        record.asset_url = None
        record.error_message = None
        record.poll_attempts = 0
        record.review_started_at = beijing_datetime() if resume_query else None
        record.next_poll_at = beijing_datetime()
        await enqueue_task_dispatch(
            db,
            task_name=REVIEW_TASK,
            args=(str(record.id),),
            queue="story_ai_default",
            message_id=record.id,
        )
    result = image_out(record, reused=True)
    record_id = record.id
    await db.commit()
    await dispatch_tasks_best_effort(db, [record_id])
    return result


async def resolve_image_ids(db: AsyncSession, user_id: UUID, references) -> dict[str, str]:
    resolved = {}
    for reference in references:
        if reference.image_id is None:
            continue
        record = await get_image(db, user_id, reference.image_id)
        if record.status != "ready":
            raise AppException("参考图片尚未审核通过，请等待审核或移除图片", code=40016)
        if record.provider_scope != provider_scope():
            raise AppException("素材服务配置已变更，请重新上传图片", code=40016)
        resolved[reference.name] = record.upload["url"]
    return resolved


async def images_by_urls(db: AsyncSession, user_id: UUID, urls: set[str]) -> dict[str, SeedanceImage]:
    """Resolve only this user's current provider scope, including verified source aliases."""
    if not urls:
        return {}
    records = (
        await db.scalars(
            select(SeedanceImage).where(
                SeedanceImage.user_id == user_id,
                SeedanceImage.provider_scope == provider_scope(),
                or_(
                    SeedanceImage.upload["url"].as_string().in_(urls),
                    *[cast(SeedanceImage.upload["source_urls"], JSONB).contains([url]) for url in urls],
                ),
            )
        )
    ).all()
    return {
        url: record
        for record in records
        for url in [record.upload["url"], *(record.upload.get("source_urls") or [])]
        if url in urls
    }


async def reviewed_video_extra(
    db: AsyncSession, user_id: UUID, model, prompt: str, extra: dict
) -> dict:
    if not is_seedance_model(model):
        return extra
    payload = apimart.build_video_generation_payload(model.model_id, prompt, extra)
    images = payload.get("image_urls", [])
    roles = payload.get("image_with_roles", [])
    urls = set(images) | {item["url"] for item in roles}
    if not urls:
        return extra
    # Includes legacy image aliases and first/last frames after provider normalization.
    # Client-supplied asset:// values cannot bypass ownership and review checks.
    records = await images_by_urls(db, user_id, urls)
    by_url = {
        url: item.asset_url for url, item in records.items()
        if item.status == "ready" and item.asset_url
    }
    if urls - by_url.keys():
        raise AppException(
            "Seedance 参考图片须登记并审核通过后使用", code=40016,
            data={
                "images": [
                    {
                        "url": url,
                        "review": image_out(records[url]).model_dump(mode="json")
                        if url in records else None,
                    }
                    for url in sorted(urls - by_url.keys())
                ]
            },
        )
    if images:
        payload["image_urls"] = [by_url[url] for url in images]
    if roles:
        payload["image_with_roles"] = [{**item, "url": by_url[item["url"]]} for item in roles]
    # Use the canonical payload to avoid retaining unreviewed source aliases.
    return {
        **payload,
        **{
            key: extra[key]
            for key in ("generation_mode", "video_mode", "capability")
            if key in extra
        },
    }
