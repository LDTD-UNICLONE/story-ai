from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin_user
from app.core.responses import success
from app.db.session import get_db
from app.models.user import User
from app.schemas.announcement import (
    AnnouncementCreateRequest,
    AnnouncementListOut,
    AnnouncementOut,
    AnnouncementPreviewOut,
    AnnouncementUpdateRequest,
)
from app.services.announcements import (
    build_announcement_preview_style,
    create_announcement,
    delete_announcement,
    get_announcement_or_404,
    list_announcements,
    update_announcement,
)

router = APIRouter(prefix="/admin/announcements")


@router.get("")
async def admin_list_announcements(
    keyword: Optional[str] = Query(default=None, max_length=255),
    announcement_type: Optional[str] = Query(default=None, max_length=32),
    display_position: Optional[str] = Query(default=None, max_length=32),
    is_enabled: Optional[bool] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    announcements, total = await list_announcements(
        db,
        keyword=keyword,
        announcement_type=announcement_type,
        display_position=display_position,
        is_enabled=is_enabled,
        page=page,
        page_size=page_size,
    )
    data = AnnouncementListOut(
        items=[AnnouncementOut.model_validate(item) for item in announcements],
        total=total,
        page=page,
        page_size=page_size,
    )
    return success(data=data.model_dump(mode="json"))


@router.post("")
async def admin_create_announcement(
    payload: AnnouncementCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    announcement = await create_announcement(db, payload)
    return success(data=AnnouncementOut.model_validate(announcement).model_dump(mode="json"), message="创建成功")


@router.get("/{announcement_id}")
async def admin_get_announcement(
    announcement_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    announcement = await get_announcement_or_404(db, announcement_id)
    return success(data=AnnouncementOut.model_validate(announcement).model_dump(mode="json"))


@router.get("/{announcement_id}/preview")
async def admin_preview_announcement(
    announcement_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    announcement = await get_announcement_or_404(db, announcement_id)
    data = AnnouncementPreviewOut(
        **AnnouncementOut.model_validate(announcement).model_dump(mode="json"),
        preview_style=build_announcement_preview_style(announcement),
    )
    return success(data=data.model_dump(mode="json"))


@router.patch("/{announcement_id}")
async def admin_update_announcement(
    announcement_id: UUID,
    payload: AnnouncementUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    announcement = await update_announcement(db, announcement_id, payload)
    return success(data=AnnouncementOut.model_validate(announcement).model_dump(mode="json"), message="更新成功")


@router.delete("/{announcement_id}")
async def admin_delete_announcement(
    announcement_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    announcement = await delete_announcement(db, announcement_id)
    return success(data=AnnouncementOut.model_validate(announcement).model_dump(mode="json"), message="删除成功")
