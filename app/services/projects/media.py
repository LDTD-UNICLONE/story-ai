"""Project-owned immutable images and shared Seedance review lookup."""

import hashlib

from sqlalchemy import func, select, text
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.exceptions import AppException
from app.models.project_media import ProjectMedia
from app.models.seedance_image import SeedanceImage
from app.schemas.project_media import ProjectMediaOut
from app.services.projects.image_reviews import register_stored_image
from app.services.projects.queries import get_project_or_404
from app.services.seedance_images import get_image, image_out, images_by_urls, provider_scope
from app.services.uploads import detect_image_content_type, upload_story_file


def _fingerprint(file):
    file.file.seek(0, 2)
    size = file.file.tell()
    file.file.seek(0)
    if size <= 0 or size > settings.max_upload_size_mb * 1024 * 1024:
        raise AppException("图片为空或超过平台上传大小限制", code=40016)
    if not detect_image_content_type(file.file):
        raise AppException("仅支持图片文件", code=40016)
    digest = hashlib.sha256()
    try:
        while chunk := file.file.read(1024 * 1024):
            digest.update(chunk)
    finally:
        file.file.seek(0)
    return digest.hexdigest()


async def _existing(db, project_id, key):
    # Serialize before OSS I/O, not just before the unique insert.
    lock = int.from_bytes(
        hashlib.sha256(f"{project_id}:{key}".encode()).digest()[:8], "big", signed=True
    )
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})
    return await db.scalar(
        select(ProjectMedia).where(
            ProjectMedia.project_id == project_id,
            ProjectMedia.source_key == key,
        )
    )


async def media_outputs(db, user_id, rows):
    urls = {row.upload["url"] for row in rows if row.media_type == "image"}
    reviews = (
        await images_by_urls(db, user_id, urls)
        if urls and settings.apimart_api_key and settings.apimart_base_url
        else {}
    )
    return [
        ProjectMediaOut(
            id=row.id,
            project_id=row.project_id,
            media_type=row.media_type,
            source_type=row.source_type,
            source_verified=row.source_verified,
            source_id=row.source_id,
            url=row.upload["url"],
            filename=row.upload["filename"],
            created_at=row.created_at,
            review=image_out(reviews[row.upload["url"]], reused=True)
            if row.upload["url"] in reviews
            else None,
        )
        for row in rows
    ]


async def _save(db, user_id, row):
    db.add(row)
    await db.flush()
    result = (await media_outputs(db, user_id, [row]))[0]
    await db.commit()
    return result


async def upload_image(db, project_id, user_id, file):
    await get_project_or_404(db, project_id, user_id)
    fingerprint = await run_in_threadpool(_fingerprint, file)
    key = f"upload:{fingerprint}"
    row = await _existing(db, project_id, key)
    if row is None:
        reviewed = None
        if settings.apimart_api_key and settings.apimart_base_url:
            reviewed = await db.scalar(
                select(SeedanceImage).where(
                    SeedanceImage.user_id == user_id,
                    SeedanceImage.provider_scope == provider_scope(),
                    SeedanceImage.sha256 == fingerprint,
                )
            )
        uploaded = (
            reviewed.upload
            if reviewed
            else (
                await upload_story_file(
                    file,
                    category=f"user-uploads/{user_id}/projects/{project_id}",
                    media_only=True,
                )
            ).model_dump(mode="json")
        )
        row = ProjectMedia(
            project_id=project_id, source_key=key, source_type="upload", upload=uploaded
        )
    return await _save(db, user_id, row)


async def import_image(db, project_id, user_id, payload):
    await get_project_or_404(db, project_id, user_id)
    source = await get_image(db, user_id, payload.source_id)
    uploaded = dict(source.upload)
    key = f"{payload.source_type}:{payload.source_id}:{hashlib.sha256(uploaded['url'].encode()).hexdigest()}"
    row = await _existing(db, project_id, key)
    if row is None:
        row = ProjectMedia(
            project_id=project_id,
            source_key=key,
            source_type=payload.source_type,
            source_id=payload.source_id,
            upload=uploaded,
        )
    return await _save(db, user_id, row)


async def get_media(db, project_id, user_id, media_id):
    await get_project_or_404(db, project_id, user_id)
    row = await db.scalar(
        select(ProjectMedia).where(
            ProjectMedia.id == media_id,
            ProjectMedia.project_id == project_id,
        )
    )
    if row is None:
        raise AppException("项目图片不存在", code=40471, status_code=404)
    return row


async def list_media(db, project_id, user_id, page, page_size):
    await get_project_or_404(db, project_id, user_id)
    condition = ProjectMedia.project_id == project_id
    total = await db.scalar(select(func.count()).select_from(ProjectMedia).where(condition))
    rows = list(
        await db.scalars(
            select(ProjectMedia)
            .where(condition)
            .order_by(ProjectMedia.created_at.desc(), ProjectMedia.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    return dict(
        items=[item.model_dump(mode="json") for item in await media_outputs(db, user_id, rows)],
        total=total,
        page=page,
        page_size=page_size,
    )


async def review_media(db, project_id, user_id, media_id):
    row = await get_media(db, project_id, user_id, media_id)
    if not row.source_verified:
        raise AppException("历史图片缺少来源凭证，请重新上传原文件后使用", code=40016)
    if row.media_type != "image":
        raise AppException("仅图片支持素材审核", code=40016)
    url = row.upload["url"]
    existing = (await images_by_urls(db, user_id, {url})).get(url)
    if existing:
        return image_out(existing, reused=True)
    # This URL was registered from an owned upload/result, never a client URL.
    # The shared service hashes the stored bytes and registers an alias without OSS copying.
    return await register_stored_image(db, user_id, url)


async def batch_media(db, project_id, user_id, ids):
    await get_project_or_404(db, project_id, user_id)
    ids = list(dict.fromkeys(ids))
    rows = {row.id: row for row in await db.scalars(
        select(ProjectMedia).where(
            ProjectMedia.project_id == project_id, ProjectMedia.id.in_(ids),
        )
    )}
    outputs = await media_outputs(db, user_id, [rows[mid] for mid in ids if mid in rows])
    return dict(
        items=[item.model_dump(mode="json") for item in outputs], total=len(outputs),
        missing_ids=[str(mid) for mid in ids if mid not in rows],
    )
