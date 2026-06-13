from pathlib import PurePosixPath
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from uuid import UUID, uuid4

from fastapi import UploadFile
from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.exceptions import AppException
from app.core.timezone import beijing_datetime
from app.integrations.oss import OssClient
from app.models.user import User
from app.models.work import UserWork, UserWorkLike, UserWorkMedia, UserWorkUpload
from app.schemas.work import (
    AdminWorkUpdateRequest,
    WorkCreateRequest,
    WorkMediaOut,
    WorkOut,
    WorkUpdateRequest,
    WorkUploadOut,
)
from app.services.uploads import detect_content_type, detect_file_type


WORK_VISIBILITIES = {"public", "private"}
WORK_STATUSES = {"draft", "published", "hidden", "deleted"}
WORK_OWNER_STATUSES = {"draft", "published"}
WORK_MEDIA_TYPES = {"image", "video"}
WORKS_OSS_PREFIX = "works"


async def upload_work_file(db: AsyncSession, user: User, file: UploadFile) -> WorkUploadOut:
    filename = file.filename or "file"
    content_type = detect_content_type(filename, file.content_type or "")
    media_type = detect_file_type(content_type)
    if media_type not in WORK_MEDIA_TYPES:
        raise AppException("作品只支持上传图片或视频", code=40040, status_code=400)

    max_size = settings.max_upload_size_mb * 1024 * 1024
    file.file.seek(0, 2)
    size = file.file.tell()
    file.file.seek(0)
    if size <= 0:
        raise AppException("上传文件不能为空", code=40007, status_code=400)
    if size > max_size:
        raise AppException(f"上传文件不能超过 {settings.max_upload_size_mb}MB", code=41300, status_code=413)

    upload_id = uuid4()
    object_key = _work_upload_object_key(user.id, upload_id, filename)
    oss_client = OssClient()
    result = await run_in_threadpool(
        oss_client.bucket.put_object,
        object_key,
        file.file,
        headers={"Content-Type": content_type},
    )
    if result.status >= 300:
        raise AppException("OSS 上传失败", code=50011, status_code=500)
    url = oss_client.public_url(object_key)
    upload = UserWorkUpload(
        id=upload_id,
        user_id=user.id,
        media_type=media_type,
        url=url,
        object_key=object_key,
        filename=filename,
        content_type=content_type,
        size=size,
        is_used=False,
    )
    db.add(upload)
    await db.commit()
    await db.refresh(upload)
    return WorkUploadOut(
        upload_id=upload.id,
        media_type=upload.media_type,
        url=upload.url,
        filename=upload.filename,
        content_type=upload.content_type,
        size=upload.size,
        preview_url=upload.url,
    )


async def create_work(db: AsyncSession, user: User, payload: WorkCreateRequest) -> WorkOut:
    upload_ids = [item.upload_id for item in payload.media_items]
    if len(set(upload_ids)) != len(upload_ids):
        raise AppException("作品媒体不能重复选择同一个上传文件", code=40041, status_code=400)

    uploads = await _uploads_for_create(db, user.id, upload_ids)
    work = UserWork(
        user_id=user.id,
        title=payload.title,
        description=payload.description,
        visibility=payload.visibility,
        status=payload.status,
        like_count=0,
        view_count=0,
        is_enabled=True,
    )
    db.add(work)

    sort_by_upload = {item.upload_id: item.sort_order for item in payload.media_items}
    try:
        await db.flush()
        work_id = work.id
        for upload in uploads:
            media_id = uuid4()
            media = UserWorkMedia(
                id=media_id,
                work_id=work_id,
                media_type=upload.media_type,
                url=upload.url,
                object_key=upload.object_key,
                filename=upload.filename,
                content_type=upload.content_type,
                size=upload.size,
                sort_order=sort_by_upload.get(upload.id, 0),
            )
            upload.is_used = True
            db.add(media)

        await db.commit()
    except Exception:
        await db.rollback()
        raise

    work = await _get_work_or_404(db, work_id)
    return await _build_work_out(db, work, user)


async def list_public_works(
    db: AsyncSession,
    user: User,
    page: int,
    page_size: int,
) -> Tuple[List[WorkOut], int]:
    conditions = [
        UserWork.is_enabled.is_(True),
        UserWork.status == "published",
        UserWork.visibility == "public",
    ]
    total = await _count_works(db, conditions)
    works = await _query_works(db, conditions, page, page_size, rank_order=True)
    return await _build_work_out_list(db, works, user), total


async def list_my_works(
    db: AsyncSession,
    user: User,
    visibility: Optional[str],
    status: Optional[str],
    page: int,
    page_size: int,
) -> Tuple[List[WorkOut], int]:
    if visibility and visibility not in WORK_VISIBILITIES:
        raise AppException("作品权限参数不正确", code=40042, status_code=400)
    if status and status not in WORK_STATUSES:
        raise AppException("作品状态参数不正确", code=40043, status_code=400)
    conditions = [UserWork.user_id == user.id, UserWork.is_enabled.is_(True)]
    if visibility:
        conditions.append(UserWork.visibility == visibility)
    if status:
        conditions.append(UserWork.status == status)
    total = await _count_works(db, conditions)
    works = await _query_works(db, conditions, page, page_size, rank_order=False)
    return await _build_work_out_list(db, works, user), total


async def list_admin_works(
    db: AsyncSession,
    user: User,
    user_id: Optional[UUID],
    visibility: Optional[str],
    status: Optional[str],
    page: int,
    page_size: int,
) -> Tuple[List[WorkOut], int]:
    conditions = []
    if user_id:
        conditions.append(UserWork.user_id == user_id)
    if visibility:
        if visibility not in WORK_VISIBILITIES:
            raise AppException("作品权限参数不正确", code=40042, status_code=400)
        conditions.append(UserWork.visibility == visibility)
    if status:
        if status not in WORK_STATUSES:
            raise AppException("作品状态参数不正确", code=40043, status_code=400)
        conditions.append(UserWork.status == status)
    total = await _count_works(db, conditions)
    works = await _query_works(db, conditions, page, page_size, rank_order=True)
    return await _build_work_out_list(db, works, user), total


async def get_work_detail(db: AsyncSession, work_id: UUID, user: User) -> WorkOut:
    work = await _get_work_or_404(db, work_id)
    _ensure_can_view_work(work, user)
    if work.status == "published" and work.visibility == "public" and work.is_enabled:
        work.view_count += 1
        await db.commit()
        await db.refresh(work)
    return await _build_work_out(db, work, user)


async def update_work(db: AsyncSession, work_id: UUID, user: User, payload: WorkUpdateRequest) -> WorkOut:
    work = await _get_work_or_404(db, work_id)
    if work.user_id != user.id:
        raise AppException("无权修改该作品", code=40320, status_code=403)
    data = payload.model_dump(exclude_unset=True)
    media_items = data.pop("media_items", None)
    target_status = data.get("status", work.status)
    if "status" in data:
        if data["status"] not in WORK_OWNER_STATUSES:
            raise AppException("作品状态参数不正确", code=40043, status_code=400)
    if target_status == "published":
        if media_items is not None and not media_items:
            raise AppException("发布作品至少需要一个媒体文件", code=40045, status_code=400)
        if media_items is None and not await _work_has_media(db, work.id):
            raise AppException("发布作品至少需要一个媒体文件", code=40045, status_code=400)

    removed_object_keys: List[str] = []
    try:
        if media_items is not None:
            removed_object_keys = await _replace_work_media(db, work.id, user.id, media_items)
        for key, value in data.items():
            setattr(work, key, value)
        work.updated_at = beijing_datetime()
        await db.flush()
        await _delete_oss_objects(removed_object_keys)
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    await db.refresh(work)
    return await _build_work_out(db, work, user)


async def admin_update_work(db: AsyncSession, work_id: UUID, user: User, payload: AdminWorkUpdateRequest) -> WorkOut:
    work = await _get_work_or_404(db, work_id)
    data = payload.model_dump(exclude_unset=True)
    if "visibility" in data and data["visibility"] not in WORK_VISIBILITIES:
        raise AppException("作品权限参数不正确", code=40042, status_code=400)
    if "status" in data and data["status"] not in WORK_STATUSES:
        raise AppException("作品状态参数不正确", code=40043, status_code=400)
    deleting = data.get("status") == "deleted" and work.status != "deleted"
    if deleting:
        await _delete_work_oss_objects(db, work.id)
    for key, value in data.items():
        setattr(work, key, value)
    work.is_enabled = work.status != "deleted"
    work.updated_at = beijing_datetime()
    await db.commit()
    await db.refresh(work)
    return await _build_work_out(db, work, user)


async def admin_approve_work(db: AsyncSession, work_id: UUID, user: User) -> WorkOut:
    work = await _get_work_or_404(db, work_id)
    if work.status == "deleted" or not work.is_enabled:
        raise AppException("已删除作品不能审核通过", code=40047, status_code=400)
    if not await _work_has_media(db, work.id):
        raise AppException("审核通过的作品至少需要一个媒体文件", code=40045, status_code=400)
    work.status = "published"
    work.is_enabled = True
    work.updated_at = beijing_datetime()
    await db.commit()
    await db.refresh(work)
    return await _build_work_out(db, work, user)


async def admin_hide_work(db: AsyncSession, work_id: UUID, user: User) -> WorkOut:
    work = await _get_work_or_404(db, work_id)
    if work.status == "deleted" or not work.is_enabled:
        raise AppException("已删除作品不能下架", code=40048, status_code=400)
    work.status = "hidden"
    work.is_enabled = True
    work.updated_at = beijing_datetime()
    await db.commit()
    await db.refresh(work)
    return await _build_work_out(db, work, user)


async def admin_delete_work(db: AsyncSession, work_id: UUID, user: User) -> WorkOut:
    work = await _get_work_or_404(db, work_id)
    if work.status != "deleted":
        await _delete_work_oss_objects(db, work.id)
    work.status = "deleted"
    work.is_enabled = False
    work.updated_at = beijing_datetime()
    await db.commit()
    await db.refresh(work)
    return await _build_work_out(db, work, user)


async def delete_work(db: AsyncSession, work_id: UUID, user: User) -> WorkOut:
    work = await _get_work_or_404(db, work_id)
    if work.user_id != user.id:
        raise AppException("无权删除该作品", code=40320, status_code=403)
    if work.status != "deleted":
        await _delete_work_oss_objects(db, work.id)
    work.status = "deleted"
    work.is_enabled = False
    work.updated_at = beijing_datetime()
    await db.commit()
    await db.refresh(work)
    return await _build_work_out(db, work, user)


async def like_work(db: AsyncSession, work_id: UUID, user: User) -> WorkOut:
    work = await _get_work_or_404(db, work_id)
    _ensure_can_view_work(work, user)
    if work.status != "published" or work.visibility != "public":
        raise AppException("当前作品不可点赞", code=40046, status_code=400)
    like = UserWorkLike(work_id=work.id, user_id=user.id)
    db.add(like)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        work = await _get_work_or_404(db, work_id)
        return await _build_work_out(db, work, user)
    work.like_count += 1
    await db.commit()
    await db.refresh(work)
    return await _build_work_out(db, work, user)


async def unlike_work(db: AsyncSession, work_id: UUID, user: User) -> WorkOut:
    work = await _get_work_or_404(db, work_id)
    result = await db.execute(delete(UserWorkLike).where(UserWorkLike.work_id == work.id, UserWorkLike.user_id == user.id))
    if result.rowcount:
        await db.execute(
            update(UserWork)
            .where(UserWork.id == work.id, UserWork.like_count > 0)
            .values(like_count=UserWork.like_count - 1, updated_at=beijing_datetime())
        )
    await db.commit()
    work = await _get_work_or_404(db, work_id)
    return await _build_work_out(db, work, user)


async def get_work_media_for_stream(
    db: AsyncSession,
    work_id: UUID,
    media_id: UUID,
    user: User,
) -> UserWorkMedia:
    work = await _get_work_or_404(db, work_id)
    _ensure_can_view_work(work, user)
    result = await db.execute(
        select(UserWorkMedia).where(UserWorkMedia.id == media_id, UserWorkMedia.work_id == work_id)
    )
    media = result.scalar_one_or_none()
    if media is None:
        raise AppException("作品媒体不存在", code=40421, status_code=404)
    return media


async def get_upload_for_preview(db: AsyncSession, upload_id: UUID, user: User) -> UserWorkUpload:
    result = await db.execute(
        select(UserWorkUpload).where(UserWorkUpload.id == upload_id, UserWorkUpload.user_id == user.id)
    )
    upload = result.scalar_one_or_none()
    if upload is None:
        raise AppException("作品上传文件不存在", code=40422, status_code=404)
    return upload


async def _uploads_for_create(
    db: AsyncSession,
    user_id: UUID,
    upload_ids: Sequence[UUID],
) -> List[UserWorkUpload]:
    if not upload_ids:
        return []
    result = await db.execute(
        select(UserWorkUpload).where(
            UserWorkUpload.id.in_(upload_ids),
            UserWorkUpload.user_id == user_id,
            UserWorkUpload.is_used.is_(False),
        )
    )
    uploads = list(result.scalars().all())
    if len(uploads) != len(upload_ids):
        raise AppException("作品上传文件不存在或已被使用", code=40044, status_code=400)
    by_id = {upload.id: upload for upload in uploads}
    return [by_id[upload_id] for upload_id in upload_ids]


async def _replace_work_media(
    db: AsyncSession,
    work_id: UUID,
    user_id: UUID,
    media_items: Sequence[Dict[str, Any]],
) -> List[str]:
    existing_media = await _work_media_items(db, work_id)
    existing_by_id = {media.id: media for media in existing_media}
    kept_media_ids: Set[UUID] = set()
    upload_ids: List[UUID] = []
    sort_by_media_id: Dict[UUID, int] = {}
    sort_by_upload_id: Dict[UUID, int] = {}

    for item in media_items:
        media_id = item.get("media_id")
        upload_id = item.get("upload_id")
        sort_order = item.get("sort_order", 0)

        if media_id:
            if media_id in kept_media_ids:
                raise AppException("作品媒体不能重复选择同一个资源", code=40049, status_code=400)
            if media_id not in existing_by_id:
                raise AppException("作品媒体不存在或不属于该作品", code=40049, status_code=400)
            kept_media_ids.add(media_id)
            sort_by_media_id[media_id] = sort_order
            continue

        if upload_id in sort_by_upload_id:
            raise AppException("作品媒体不能重复选择同一个上传文件", code=40041, status_code=400)
        upload_ids.append(upload_id)
        sort_by_upload_id[upload_id] = sort_order

    uploads = await _uploads_for_create(db, user_id, upload_ids)
    removed_object_keys: List[str] = []

    for media_id, sort_order in sort_by_media_id.items():
        media = existing_by_id[media_id]
        media.sort_order = sort_order
        media.updated_at = beijing_datetime()

    for media in existing_media:
        if media.id in kept_media_ids:
            continue
        if media.object_key:
            removed_object_keys.append(media.object_key)
        if media.thumbnail_object_key:
            removed_object_keys.append(media.thumbnail_object_key)
        await db.delete(media)

    for upload in uploads:
        media = UserWorkMedia(
            id=uuid4(),
            work_id=work_id,
            media_type=upload.media_type,
            url=upload.url,
            object_key=upload.object_key,
            filename=upload.filename,
            content_type=upload.content_type,
            size=upload.size,
            sort_order=sort_by_upload_id.get(upload.id, 0),
        )
        upload.is_used = True
        db.add(media)

    return removed_object_keys


async def _get_work_or_404(db: AsyncSession, work_id: UUID) -> UserWork:
    result = await db.execute(select(UserWork).where(UserWork.id == work_id))
    work = result.scalar_one_or_none()
    if work is None:
        raise AppException("作品不存在", code=40420, status_code=404)
    return work


def _ensure_can_view_work(work: UserWork, user: User) -> None:
    if user.is_admin:
        return
    if not work.is_enabled:
        raise AppException("作品不存在", code=40420, status_code=404)
    if work.user_id == user.id:
        return
    if work.status != "published":
        raise AppException("作品不存在", code=40420, status_code=404)
    if work.visibility == "public":
        return
    raise AppException("无权查看该作品", code=40321, status_code=403)


async def _count_works(db: AsyncSession, conditions: List[Any]) -> int:
    result = await db.execute(select(func.count()).select_from(UserWork).where(*conditions))
    return int(result.scalar_one() or 0)


async def _query_works(
    db: AsyncSession,
    conditions: List[Any],
    page: int,
    page_size: int,
    *,
    rank_order: bool,
) -> List[UserWork]:
    order_by = (
        (UserWork.like_count.desc(), UserWork.created_at.desc(), UserWork.id.desc())
        if rank_order
        else (UserWork.created_at.desc(), UserWork.id.desc())
    )
    result = await db.execute(
        select(UserWork)
        .where(*conditions)
        .order_by(*order_by)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(result.scalars().all())


async def _build_work_out_list(db: AsyncSession, works: List[UserWork], user: User) -> List[WorkOut]:
    if not works:
        return []
    work_ids = [work.id for work in works]
    media_result = await db.execute(
        select(UserWorkMedia)
        .where(UserWorkMedia.work_id.in_(work_ids))
        .order_by(UserWorkMedia.work_id.asc(), UserWorkMedia.sort_order.asc(), UserWorkMedia.created_at.asc())
    )
    media_by_work: Dict[UUID, List[UserWorkMedia]] = {work_id: [] for work_id in work_ids}
    for media in media_result.scalars().all():
        media_by_work.setdefault(media.work_id, []).append(media)

    like_result = await db.execute(
        select(UserWorkLike.work_id).where(UserWorkLike.work_id.in_(work_ids), UserWorkLike.user_id == user.id)
    )
    liked_work_ids = set(like_result.scalars().all())
    return [_build_work_out_from_items(work, media_by_work.get(work.id, []), work.id in liked_work_ids) for work in works]


async def _build_work_out(db: AsyncSession, work: UserWork, user: User) -> WorkOut:
    media_items = await _work_media_items(db, work.id)
    liked = await _liked_by_user(db, work.id, user.id)
    return _build_work_out_from_items(work, media_items, liked)


def _build_work_out_from_items(
    work: UserWork,
    media_items: List[UserWorkMedia],
    liked: bool,
) -> WorkOut:
    return WorkOut(
        id=work.id,
        user_id=work.user_id,
        title=work.title,
        description=work.description,
        visibility=work.visibility,
        status=work.status,
        like_count=work.like_count,
        view_count=work.view_count,
        liked_by_me=liked,
        media_items=[_media_out(work.id, media) for media in media_items],
        created_at=work.created_at,
        updated_at=work.updated_at,
    )


async def _work_media_items(db: AsyncSession, work_id: UUID) -> List[UserWorkMedia]:
    result = await db.execute(
        select(UserWorkMedia)
        .where(UserWorkMedia.work_id == work_id)
        .order_by(UserWorkMedia.sort_order.asc(), UserWorkMedia.created_at.asc())
    )
    return list(result.scalars().all())


async def _liked_by_user(db: AsyncSession, work_id: UUID, user_id: UUID) -> bool:
    result = await db.execute(
        select(exists().where(UserWorkLike.work_id == work_id, UserWorkLike.user_id == user_id))
    )
    return bool(result.scalar())


async def _work_has_media(db: AsyncSession, work_id: UUID) -> bool:
    result = await db.execute(select(exists().where(UserWorkMedia.work_id == work_id)))
    return bool(result.scalar())


async def _delete_work_oss_objects(db: AsyncSession, work_id: UUID) -> None:
    result = await db.execute(
        select(UserWorkMedia.object_key, UserWorkMedia.thumbnail_object_key).where(UserWorkMedia.work_id == work_id)
    )
    object_keys: List[str] = []
    for object_key, thumbnail_object_key in result.all():
        if object_key:
            object_keys.append(object_key)
        if thumbnail_object_key:
            object_keys.append(thumbnail_object_key)
    await _delete_oss_objects(object_keys)


async def _delete_oss_objects(object_keys: Sequence[str]) -> None:
    object_keys = list(dict.fromkeys(object_keys))
    if not object_keys:
        return
    oss_client = OssClient()
    for object_key in object_keys:
        await run_in_threadpool(oss_client.delete_object, object_key)


def _media_out(work_id: UUID, media: UserWorkMedia) -> WorkMediaOut:
    thumbnail_url = _oss_public_url(media.thumbnail_object_key) if media.thumbnail_object_key else None
    return WorkMediaOut(
        id=media.id,
        media_type=media.media_type,
        url=media.url,
        filename=media.filename,
        content_type=media.content_type,
        size=media.size,
        width=media.width,
        height=media.height,
        duration_seconds=media.duration_seconds,
        sort_order=media.sort_order,
        stream_url=media.url,
        thumbnail_url=thumbnail_url,
        created_at=media.created_at,
    )


def _work_upload_object_key(user_id: UUID, upload_id: UUID, filename: str) -> str:
    suffix = PurePosixPath(filename).suffix
    return _works_object_key(str(user_id), "uploads", f"{upload_id}{suffix}")


def _works_object_key(*parts: str) -> str:
    root = (settings.oss_root_directory or "").strip("/")
    key_parts = [part.strip("/") for part in parts if part.strip("/")]
    if root:
        key_parts.insert(0, root)
    key_parts.insert(1 if root else 0, WORKS_OSS_PREFIX)
    return str(PurePosixPath(*key_parts))


def _oss_public_url(object_key: str) -> str:
    if settings.oss_public_base_url:
        return f"{settings.oss_public_base_url.rstrip('/')}/{object_key.lstrip('/')}"
    endpoint = settings.oss_endpoint.removeprefix("https://").removeprefix("http://").rstrip("/")
    return f"https://{settings.oss_bucket_name}.{endpoint}/{object_key.lstrip('/')}"
