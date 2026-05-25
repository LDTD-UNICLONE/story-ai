from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin_user
from app.core.responses import success
from app.db.session import get_db
from app.models.user import User
from app.schemas.points import RechargeOrderListOut, RechargeOrderOut, RechargeRefundRequest
from app.services.recharges import (
    get_recharge_order_or_404,
    list_all_recharge_orders,
    refund_recharge_order,
    sync_recharge_order_from_wechat,
)

router = APIRouter(prefix="/admin/recharges")


@router.get("")
async def admin_recharge_orders(
    user_id: Optional[UUID] = Query(default=None),
    status: Optional[str] = Query(default=None, max_length=32),
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
    data = RechargeOrderListOut(
        items=[RechargeOrderOut.model_validate(order) for order in orders],
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
    return success(data=RechargeOrderOut.model_validate(order).model_dump(mode="json"))


@router.post("/{order_id}/sync")
async def admin_sync_recharge_order(
    order_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    order = await get_recharge_order_or_404(db, order_id)
    order = await sync_recharge_order_from_wechat(db, order)
    return success(data=RechargeOrderOut.model_validate(order).model_dump(mode="json"), message="同步成功")


@router.post("/{order_id}/refund")
async def admin_refund_recharge_order(
    order_id: UUID,
    payload: RechargeRefundRequest,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    order = await refund_recharge_order(db, order_id=order_id, reason=payload.reason)
    return success(
        data=RechargeOrderOut.model_validate(order).model_dump(mode="json"),
        message="退款已提交",
    )
