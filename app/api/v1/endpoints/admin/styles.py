from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin_user
from app.core.responses import success
from app.db.session import get_db
from app.models.user import User
from app.schemas.style import StyleCreateRequest, StyleListOut, StyleOut, StyleUpdateRequest
from app.services.styles import create_style, delete_style, get_style_or_404, list_styles, update_style

router = APIRouter(prefix="/admin/styles")


@router.get("")
async def admin_list_styles(
    keyword: Optional[str] = Query(default=None, max_length=255),
    is_enabled: Optional[bool] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    styles, total = await list_styles(
        db,
        keyword=keyword,
        is_enabled=is_enabled,
        page=page,
        page_size=page_size,
    )
    data = StyleListOut(
        items=[StyleOut.model_validate(item) for item in styles],
        total=total,
        page=page,
        page_size=page_size,
    )
    return success(data=data.model_dump(mode="json"))


@router.post("")
async def admin_create_style(
    payload: StyleCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    style = await create_style(db, payload)
    return success(data=StyleOut.model_validate(style).model_dump(mode="json"), message="创建成功")


@router.get("/{style_id}")
async def admin_get_style(
    style_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    style = await get_style_or_404(db, style_id)
    return success(data=StyleOut.model_validate(style).model_dump(mode="json"))


@router.patch("/{style_id}")
async def admin_update_style(
    style_id: UUID,
    payload: StyleUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    style = await update_style(db, style_id, payload)
    return success(data=StyleOut.model_validate(style).model_dump(mode="json"), message="更新成功")


@router.delete("/{style_id}")
async def admin_delete_style(
    style_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    await delete_style(db, style_id)
    return success(message="删除成功")
