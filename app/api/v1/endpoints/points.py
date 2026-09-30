from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.responses import success
from app.db.session import get_db
from app.models.user import User
from app.schemas.points import (
    PointsBalanceOut,
    PointsTransactionListOut,
    PointsTransactionOut,
    RechargeCreateRequest,
    RechargeOrderListOut,
    RechargeOrderOut,
)
from app.services.billing.points import get_user_points_balance, list_user_points_transactions
from app.services.billing.recharges import (
    create_recharge_order,
    get_user_recharge_order_or_404,
    handle_wechat_pay_notify,
    list_my_recharge_orders,
    sync_recharge_order_from_wechat,
)

router = APIRouter(prefix="/points")


@router.get("/balance", summary="获取当前用户的积分余额")
async def my_points_balance(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    balance = await get_user_points_balance(db, current_user.id)
    return success(data=PointsBalanceOut(points_balance=balance).model_dump(mode="json"))


@router.get("/transactions")
async def my_points_transactions(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    transactions, total = await list_user_points_transactions(
        db,
        user_id=current_user.id,
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


@router.post("/recharges")
async def create_my_recharge_order(
    payload: RechargeCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    order = await create_recharge_order(
        db, user_id=current_user.id, amount_yuan=payload.amount_yuan
    )
    return success(
        data=RechargeOrderOut.model_validate(order).model_dump(mode="json"),
        message="充值订单已创建",
    )


@router.get("/recharges")
async def my_recharge_orders(
    status: Optional[str] = Query(default=None, max_length=32),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    orders, total = await list_my_recharge_orders(
        db,
        user_id=current_user.id,
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


@router.get("/recharges/{order_id}")
async def my_recharge_order_detail(
    order_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    order = await get_user_recharge_order_or_404(db, order_id=order_id, user_id=current_user.id)
    return success(data=RechargeOrderOut.model_validate(order).model_dump(mode="json"))


@router.post("/recharges/{order_id}/sync")
async def sync_my_recharge_order(
    order_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    order = await get_user_recharge_order_or_404(db, order_id=order_id, user_id=current_user.id)
    order = await sync_recharge_order_from_wechat(db, order)
    return success(
        data=RechargeOrderOut.model_validate(order).model_dump(mode="json"), message="同步成功"
    )


@router.post("/wechat/notify")
async def wechat_pay_notify(request: Request, db: AsyncSession = Depends(get_db)):
    await handle_wechat_pay_notify(db, request)
    return {"code": "SUCCESS", "message": "成功"}
