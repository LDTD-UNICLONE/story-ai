from typing import List, Optional, Tuple
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.announcement_security import (
    sanitize_announcement_content,
    validate_announcement_url,
)
from app.core.exceptions import AppException
from app.core.timezone import beijing_datetime
from app.models.announcement import Announcement
from app.schemas.announcement import AnnouncementCreateRequest, AnnouncementUpdateRequest


DEFAULT_PREVIEW_STYLE = {
    "layout": "card",
    "theme": "light",
    "title_color": "#111827",
    "content_color": "#374151",
    "background_color": "#ffffff",
    "accent_color": "#2563eb",
    "image_position": "top",
}


async def list_announcements(
    db: AsyncSession,
    keyword: Optional[str],
    announcement_type: Optional[str],
    display_position: Optional[str],
    is_enabled: Optional[bool],
    page: int,
    page_size: int,
) -> Tuple[List[Announcement], int]:
    conditions = []
    if keyword:
        pattern = f"%{keyword}%"
        conditions.append(
            or_(Announcement.title.ilike(pattern), Announcement.content.ilike(pattern))
        )
    if announcement_type:
        conditions.append(Announcement.announcement_type == announcement_type)
    if display_position:
        conditions.append(Announcement.display_position == display_position)
    if is_enabled is not None:
        conditions.append(Announcement.is_enabled == is_enabled)

    count_query = select(func.count()).select_from(Announcement)
    query = select(Announcement)
    if conditions:
        count_query = count_query.where(*conditions)
        query = query.where(*conditions)

    total_result = await db.execute(count_query)
    total = total_result.scalar_one()
    result = await db.execute(
        query.order_by(Announcement.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(result.scalars().all()), total


async def list_active_announcements(
    db: AsyncSession,
    display_position: Optional[str] = None,
    limit: int = 20,
) -> List[Announcement]:
    now = beijing_datetime()
    conditions = [
        Announcement.is_enabled.is_(True),
        or_(Announcement.start_at.is_(None), Announcement.start_at <= now),
        or_(Announcement.end_at.is_(None), Announcement.end_at >= now),
    ]
    if display_position:
        conditions.append(Announcement.display_position == display_position)

    result = await db.execute(
        select(Announcement)
        .where(*conditions)
        .order_by(Announcement.sort_order.desc(), Announcement.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def get_announcement_or_404(db: AsyncSession, announcement_id: UUID) -> Announcement:
    result = await db.execute(select(Announcement).where(Announcement.id == announcement_id))
    announcement = result.scalar_one_or_none()
    if announcement is None:
        raise AppException("公告不存在", code=40420, status_code=404)
    return announcement


async def get_active_announcement_or_404(db: AsyncSession, announcement_id: UUID) -> Announcement:
    announcement = await get_announcement_or_404(db, announcement_id)
    now = beijing_datetime()
    if (
        not announcement.is_enabled
        or (announcement.start_at and announcement.start_at > now)
        or (announcement.end_at and announcement.end_at < now)
    ):
        raise AppException("公告不存在或未发布", code=40420, status_code=404)
    return announcement


async def create_announcement(db: AsyncSession, payload: AnnouncementCreateRequest) -> Announcement:
    _validate_publish_time(payload.start_at, payload.end_at)
    create_data = payload.model_dump()
    _sanitize_announcement_write_data(create_data)
    announcement = Announcement(**create_data)
    db.add(announcement)
    await db.commit()
    await db.refresh(announcement)
    return announcement


async def update_announcement(
    db: AsyncSession,
    announcement_id: UUID,
    payload: AnnouncementUpdateRequest,
) -> Announcement:
    announcement = await get_announcement_or_404(db, announcement_id)
    update_data = payload.model_dump(exclude_unset=True)
    start_at = update_data.get("start_at", announcement.start_at)
    end_at = update_data.get("end_at", announcement.end_at)
    _validate_publish_time(start_at, end_at)
    if "content" in update_data or "content_format" in update_data:
        update_data["content"] = sanitize_announcement_content(
            update_data.get("content", announcement.content),
            update_data.get("content_format", announcement.content_format),
        )
    if "image_url" in update_data:
        update_data["image_url"] = validate_announcement_url(
            update_data["image_url"], "公告图片地址"
        )
    if "link_url" in update_data:
        update_data["link_url"] = validate_announcement_url(
            update_data["link_url"], "公告跳转地址"
        )
    for field, value in update_data.items():
        setattr(announcement, field, value)
    announcement.updated_at = beijing_datetime()
    await db.commit()
    await db.refresh(announcement)
    return announcement


async def delete_announcement(db: AsyncSession, announcement_id: UUID) -> Announcement:
    announcement = await get_announcement_or_404(db, announcement_id)
    announcement.is_enabled = False
    announcement.updated_at = beijing_datetime()
    await db.commit()
    await db.refresh(announcement)
    return announcement


def build_announcement_preview_style(announcement: Announcement) -> dict:
    return {**DEFAULT_PREVIEW_STYLE, **(announcement.style_config or {})}


def _validate_publish_time(start_at, end_at) -> None:
    if start_at and end_at and start_at > end_at:
        raise AppException("公告开始时间不能晚于结束时间", code=40020, status_code=400)


def _sanitize_announcement_write_data(data: dict) -> None:
    data["content"] = sanitize_announcement_content(data["content"], data["content_format"])
    data["image_url"] = validate_announcement_url(data.get("image_url"), "公告图片地址")
    data["link_url"] = validate_announcement_url(data.get("link_url"), "公告跳转地址")
