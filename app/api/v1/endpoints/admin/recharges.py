from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin_user
from app.core.responses import success
from app.db.session import get_db
from app.models.points import UserRechargeOrder
from app.models.user import User
from app.schemas.points import (
    AdminRechargeOrderListOut,
    AdminRechargeOrderOut,
    PointsRecordUserOut,
    RechargeOrderOut,
    RechargeRefundRequest,
)
from app.services.billing.recharges import (
    get_recharge_order_or_404,
    list_all_recharge_orders,
    refund_recharge_order,
    sync_recharge_order_status,
)

router = APIRouter(prefix="/admin/recharges")


@router.get("")
async def admin_recharge_orders(
    user_id: Optional[UUID] = Query(default=None),
    status: Optional[str] = Query(
        default=None,
        pattern="^(pending|paid|refunding|refunded)$",
    ),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    orders, total = await list_all_recharge_orders(
        db,
        user_id=user_id,
        status=status,
        page=page,
        page_size=page_size,
    )
    users = await _load_order_users(db, orders)
    data = AdminRechargeOrderListOut(
        items=[_build_admin_recharge_order(order, users[order.user_id]) for order in orders],
        total=total,
        page=page,
        page_size=page_size,
    )
    return success(data=data.model_dump(mode="json"))


@router.get("/{order_id}")
async def admin_recharge_order_detail(
    order_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    order = await get_recharge_order_or_404(db, order_id)
    users = await _load_order_users(db, [order])
    data = _build_admin_recharge_order(order, users[order.user_id])
    return success(data=data.model_dump(mode="json"))


@router.post("/{order_id}/sync")
async def admin_sync_recharge_order(
    order_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    order = await get_recharge_order_or_404(db, order_id)
    order = await sync_recharge_order_status(db, order)
    users = await _load_order_users(db, [order])
    data = _build_admin_recharge_order(order, users[order.user_id])
    return success(data=data.model_dump(mode="json"), message="同步成功")


@router.post("/{order_id}/refund")
async def admin_refund_recharge_order(
    order_id: UUID,
    payload: RechargeRefundRequest,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    order = await refund_recharge_order(db, order_id=order_id, reason=payload.reason)
    users = await _load_order_users(db, [order])
    data = _build_admin_recharge_order(order, users[order.user_id])
    return success(
        data=data.model_dump(mode="json"),
        message="退款已提交",
    )


async def _load_order_users(
    db: AsyncSession,
    orders: list[UserRechargeOrder],
) -> dict[UUID, User]:
    user_ids = {order.user_id for order in orders}
    if not user_ids:
        return {}
    result = await db.execute(select(User).where(User.id.in_(user_ids)))
    return {user.id: user for user in result.scalars().all()}


def _build_admin_recharge_order(order: UserRechargeOrder, user: User) -> AdminRechargeOrderOut:
    data = RechargeOrderOut.model_validate(order).model_dump(mode="python")
    return AdminRechargeOrderOut(
        **data,
        user=PointsRecordUserOut(
            id=user.id,
            account=user.account,
            nickname=user.nickname,
            avatar=user.avatar,
            phone=user.phone,
            email=user.email,
        ),
    )
