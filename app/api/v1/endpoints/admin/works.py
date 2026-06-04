from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin_user
from app.core.responses import success
from app.db.session import get_db
from app.models.user import User
from app.schemas.work import AdminWorkListOut, AdminWorkUpdateRequest
from app.services.works import (
    admin_approve_work,
    admin_delete_work,
    admin_hide_work,
    admin_update_work,
    get_work_detail,
    list_admin_works,
)

router = APIRouter(prefix="/admin/works")


@router.get("")
async def admin_works(
    user_id: Optional[UUID] = Query(default=None),
    visibility: Optional[str] = Query(default=None, pattern="^(public|private)$"),
    status: Optional[str] = Query(default=None, pattern="^(draft|published|hidden|deleted)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    works, total = await list_admin_works(db, current_admin, user_id, visibility, status, page, page_size)
    data = AdminWorkListOut(items=works, total=total, page=page, page_size=page_size)
    return success(data=data.model_dump(mode="json"))


@router.get("/{work_id}")
async def admin_work_detail(
    work_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    work = await get_work_detail(db, work_id, current_admin)
    return success(data=work.model_dump(mode="json"))


@router.patch("/{work_id}")
async def admin_update_user_work(
    work_id: UUID,
    payload: AdminWorkUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    work = await admin_update_work(db, work_id, current_admin, payload)
    return success(data=work.model_dump(mode="json"), message="更新成功")


@router.post("/{work_id}/approve")
async def admin_approve_user_work(
    work_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    work = await admin_approve_work(db, work_id, current_admin)
    return success(data=work.model_dump(mode="json"), message="审核通过")


@router.post("/{work_id}/hide")
async def admin_hide_user_work(
    work_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    work = await admin_hide_work(db, work_id, current_admin)
    return success(data=work.model_dump(mode="json"), message="已下架")


@router.delete("/{work_id}")
async def admin_delete_user_work(
    work_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    work = await admin_delete_work(db, work_id, current_admin)
    return success(data=work.model_dump(mode="json"), message="删除成功")
