from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.responses import success
from app.db.session import get_db
from app.schemas.announcement import AnnouncementBaseOut
from app.services.announcements import get_active_announcement_or_404, list_active_announcements

router = APIRouter(prefix="/announcements")


@router.get("")
async def announcement_options(
    display_position: Optional[str] = Query(default=None, max_length=32),
    limit: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    announcements = await list_active_announcements(
        db,
        display_position=display_position,
        limit=limit,
    )
    data = [
        AnnouncementBaseOut.model_validate(item).model_dump(mode="json") for item in announcements
    ]
    return success(data=data)


@router.get("/{announcement_id}")
async def announcement_detail(
    announcement_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    announcement = await get_active_announcement_or_404(db, announcement_id)
    return success(data=AnnouncementBaseOut.model_validate(announcement).model_dump(mode="json"))
