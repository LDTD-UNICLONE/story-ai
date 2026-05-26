from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin_user
from app.core.responses import success
from app.db.session import get_db
from app.models.user import User
from app.schemas.points import (
    AdminPointsAdjustRequest,
    PointsTransactionListOut,
    PointsTransactionOut,
)
from app.schemas.user import (
    AdminPasswordResetRequest,
    AdminUserCreateRequest,
    AdminUserUpdateRequest,
    UserListOut,
    UserOut,
)
from app.services.admin_users import (
    create_user,
    delete_user,
    get_user_or_404,
    list_users,
    reset_user_password,
    update_user,
)
from app.services.points import change_user_points, list_user_points_transactions

router = APIRouter(prefix="/admin/users")


@router.get("")
async def admin_list_users(
    keyword: Optional[str] = Query(default=None, max_length=255),
    is_enabled: Optional[bool] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    users, total = await list_users(
        db,
        keyword=keyword,
        is_enabled=is_enabled,
        page=page,
        page_size=page_size,
    )
    data = UserListOut(
        items=[UserOut.model_validate(user) for user in users],
        total=total,
        page=page,
        page_size=page_size,
    )
    return success(data=data.model_dump(mode="json"))


@router.post("")
async def admin_create_user(
    payload: AdminUserCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    user = await create_user(db, payload)
    return success(data=UserOut.model_validate(user).model_dump(mode="json"), message="创建成功")


@router.get("/{user_id}")
async def admin_get_user(
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    user = await get_user_or_404(db, user_id)
    return success(data=UserOut.model_validate(user).model_dump(mode="json"))


@router.patch("/{user_id}")
async def admin_update_user(
    user_id: UUID,
    payload: AdminUserUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    user = await update_user(db, user_id, payload)
    return success(data=UserOut.model_validate(user).model_dump(mode="json"), message="更新成功")


@router.patch("/{user_id}/password")
async def admin_reset_user_password(
    user_id: UUID,
    payload: AdminPasswordResetRequest,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    user = await reset_user_password(db, user_id, payload.password)
    return success(data=UserOut.model_validate(user).model_dump(mode="json"), message="密码已重置")


@router.post("/{user_id}/points")
async def admin_adjust_user_points(
    user_id: UUID,
    payload: AdminPointsAdjustRequest,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    transaction = await change_user_points(
        db=db,
        user_id=user_id,
        amount=payload.amount,
        transaction_type="admin_adjust",
        remark=payload.remark,
    )
    return success(
        data=PointsTransactionOut.model_validate(transaction).model_dump(mode="json"),
        message="积分调整成功",
    )


@router.get("/{user_id}/points/transactions")
async def admin_list_user_points_transactions(
    user_id: UUID,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    await get_user_or_404(db, user_id)
    transactions, total = await list_user_points_transactions(
        db,
        user_id=user_id,
        page=page,
        page_size=page_size,
    )
    data = PointsTransactionListOut(
        items=[PointsTransactionOut.model_validate(item) for item in transactions],
        total=total,
        page=page,
        page_size=page_size,
    )
    return success(data=data.model_dump(mode="json"))


@router.delete("/{user_id}")
async def admin_delete_user(
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    await delete_user(db, user_id, current_admin.id)
    return success(message="删除成功")
