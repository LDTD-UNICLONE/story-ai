import json
import secrets
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Optional, Tuple
from uuid import UUID

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import AppException
from app.core.timezone import beijing_datetime
from app.integrations.wechat_pay import wechat_pay_client
from app.models.points import UserRechargeOrder
from app.services.billing.points import change_user_points, ensure_user_points_enough

RECHARGE_POINTS_PER_YUAN = 10


async def create_recharge_order(
    db: AsyncSession,
    *,
    user_id: UUID,
    amount_yuan: Decimal,
) -> UserRechargeOrder:
    if not settings.wechat_pay_mock_enabled and not settings.wechat_pay_notify_url:
        raise AppException("微信支付回调地址未配置", code=50032, status_code=500)

    amount_cents = _amount_yuan_to_cents(amount_yuan)
    points_amount = _amount_cents_to_points(amount_cents)
    out_trade_no = _out_trade_no()
    description = f"积分充值 {points_amount} 积分"

    pay_result = await wechat_pay_client.create_native_order(
        out_trade_no=out_trade_no,
        description=description,
        amount_cents=amount_cents,
        notify_url=settings.wechat_pay_notify_url,
    )
    code_url = pay_result.get("code_url")
    if not code_url:
        raise AppException("支付二维码生成失败，请稍后再试", code=50231, status_code=502)

    order = UserRechargeOrder(
        user_id=user_id,
        out_trade_no=out_trade_no,
        amount_cents=amount_cents,
        points_amount=points_amount,
        status="pending",
        code_url=code_url,
        description=description,
        extra={
            "pay_type": "wechat_native",
            "trade_type": "NATIVE",
            **({"wechat_create_response": pay_result} if settings.app_debug else {}),
        },
    )
    db.add(order)
    await db.commit()
    await db.refresh(order)
    return order


async def list_my_recharge_orders(
    db: AsyncSession,
    *,
    user_id: UUID,
    status: Optional[str],
    page: int,
    page_size: int,
) -> Tuple[List[UserRechargeOrder], int]:
    await purge_expired_pending_recharge_orders(db, user_id=user_id, limit=100)
    await sync_user_pending_recharge_orders(db, user_id=user_id, limit=5)

    query = select(UserRechargeOrder).where(UserRechargeOrder.user_id == user_id)
    if status:
        query = query.where(UserRechargeOrder.status == status)

    count_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = count_result.scalar_one()
    result = await db.execute(
        query.order_by(UserRechargeOrder.created_at.desc(), UserRechargeOrder.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(result.scalars().all()), total


async def list_all_recharge_orders(
    db: AsyncSession,
    *,
    user_id: Optional[UUID],
    status: Optional[str],
    page: int,
    page_size: int,
) -> Tuple[List[UserRechargeOrder], int]:
    query = select(UserRechargeOrder)
    if user_id:
        query = query.where(UserRechargeOrder.user_id == user_id)
    if status:
        query = query.where(UserRechargeOrder.status == status)

    count_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = count_result.scalar_one()
    result = await db.execute(
        query.order_by(UserRechargeOrder.created_at.desc(), UserRechargeOrder.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(result.scalars().all()), total


async def get_user_recharge_order_or_404(
    db: AsyncSession,
    *,
    order_id: UUID,
    user_id: UUID,
) -> UserRechargeOrder:
    result = await db.execute(
        select(UserRechargeOrder).where(
            UserRechargeOrder.id == order_id,
            UserRechargeOrder.user_id == user_id,
        )
    )
    order = result.scalar_one_or_none()
    if order is None:
        raise AppException("充值订单不存在", code=40440, status_code=404)
    order = await resolve_expired_pending_recharge_order(db, order)
    if order is None:
        raise AppException("充值订单不存在", code=40440, status_code=404)
    if order.status == "pending":
        order = await sync_recharge_order_from_wechat(db, order)
    elif order.status == "refunding":
        order = await sync_refund_order_from_wechat(db, order)
    return order


async def get_recharge_order_or_404(db: AsyncSession, order_id: UUID) -> UserRechargeOrder:
    order = await db.get(UserRechargeOrder, order_id)
    if order is None:
        raise AppException("充值订单不存在", code=40440, status_code=404)
    order = await resolve_expired_pending_recharge_order(db, order)
    if order is None:
        raise AppException("充值订单不存在", code=40440, status_code=404)
    return order


async def sync_recharge_order_status(
    db: AsyncSession,
    order: UserRechargeOrder,
) -> UserRechargeOrder:
    if order.status == "pending":
        return await sync_recharge_order_from_wechat(db, order)
    if order.status == "refunding":
        return await sync_refund_order_from_wechat(db, order)
    return order


async def handle_wechat_pay_notify(db: AsyncSession, request: Request) -> None:
    body = await request.body()
    headers = {key.lower(): value for key, value in request.headers.items()}
    wechat_pay_client.verify_notify_signature(headers=headers, body=body)
    payload = json.loads(body.decode("utf-8"))
    if payload.get("event_type") != "TRANSACTION.SUCCESS":
        raise AppException("不支持的微信支付回调事件", code=40042, status_code=400)
    resource = payload.get("resource")
    if not isinstance(resource, dict):
        raise AppException("微信支付回调资源缺失", code=40042, status_code=400)
    resource_data = wechat_pay_client.decrypt_notify_resource(resource)
    await mark_recharge_paid(db, resource_data)


async def sync_user_pending_recharge_orders(
    db: AsyncSession,
    *,
    user_id: UUID,
    limit: int = 5,
) -> None:
    await purge_expired_pending_recharge_orders(db, user_id=user_id, limit=max(limit, 100))
    result = await db.execute(
        select(UserRechargeOrder)
        .where(UserRechargeOrder.user_id == user_id, UserRechargeOrder.status == "pending")
        .order_by(UserRechargeOrder.created_at.desc())
        .limit(limit)
    )
    for order in result.scalars().all():
        await sync_recharge_order_from_wechat(db, order)

    refund_result = await db.execute(
        select(UserRechargeOrder)
        .where(UserRechargeOrder.user_id == user_id, UserRechargeOrder.status == "refunding")
        .order_by(UserRechargeOrder.updated_at.asc())
        .limit(limit)
    )
    for order in refund_result.scalars().all():
        await sync_refund_order_from_wechat(db, order)


async def purge_expired_pending_recharge_orders(
    db: AsyncSession,
    *,
    user_id: Optional[UUID] = None,
    limit: int = 100,
) -> int:
    query = (
        select(UserRechargeOrder)
        .where(
            UserRechargeOrder.status == "pending",
            UserRechargeOrder.created_at <= _pending_recharge_expires_before(),
        )
        .order_by(UserRechargeOrder.created_at.asc())
        .limit(limit)
    )
    if user_id:
        query = query.where(UserRechargeOrder.user_id == user_id)

    result = await db.execute(query)
    deleted_count = 0
    for order in result.scalars().all():
        resolved_order = await resolve_expired_pending_recharge_order(db, order)
        if resolved_order is None:
            deleted_count += 1
    return deleted_count


async def resolve_expired_pending_recharge_order(
    db: AsyncSession,
    order: UserRechargeOrder,
) -> Optional[UserRechargeOrder]:
    if order.status != "pending" or not _is_pending_recharge_expired(order):
        return order

    synced_order = await sync_recharge_order_from_wechat(db, order)
    if synced_order.status != "pending":
        return synced_order

    result = await db.execute(
        select(UserRechargeOrder).where(UserRechargeOrder.id == synced_order.id).with_for_update()
    )
    locked_order = result.scalar_one_or_none()
    if locked_order is None:
        return None
    if locked_order.status != "pending":
        await db.commit()
        await db.refresh(locked_order)
        return locked_order

    await db.delete(locked_order)
    await db.commit()
    return None


async def sync_recharge_order_from_wechat(
    db: AsyncSession,
    order: UserRechargeOrder,
) -> UserRechargeOrder:
    if order.status != "pending":
        return order
    data = await wechat_pay_client.query_order_by_out_trade_no(order.out_trade_no)
    if data.get("trade_state") == "SUCCESS":
        return await mark_recharge_paid(db, data)

    result = await db.execute(
        select(UserRechargeOrder).where(UserRechargeOrder.id == order.id).with_for_update()
    )
    locked_order = result.scalar_one_or_none()
    if locked_order is None:
        raise AppException("充值订单不存在", code=40440, status_code=404)
    locked_order.extra = {**(locked_order.extra or {}), "wechat_query_response": data}
    await db.commit()
    await db.refresh(locked_order)
    return locked_order


async def sync_refund_order_from_wechat(
    db: AsyncSession,
    order: UserRechargeOrder,
) -> UserRechargeOrder:
    if order.status != "refunding":
        return order
    if not order.out_refund_no:
        raise AppException("充值订单缺少退款单号", code=40043, status_code=400)

    data = await wechat_pay_client.query_refund_by_out_refund_no(order.out_refund_no)
    result = await db.execute(
        select(UserRechargeOrder).where(UserRechargeOrder.id == order.id).with_for_update()
    )
    locked_order = result.scalar_one_or_none()
    if locked_order is None:
        raise AppException("充值订单不存在", code=40440, status_code=404)

    refund_status = data.get("status")
    locked_order.refund_id = data.get("refund_id") or locked_order.refund_id
    locked_order.extra = {**(locked_order.extra or {}), "wechat_refund_query_response": data}
    if refund_status == "SUCCESS":
        locked_order.status = "refunded"
        locked_order.refunded_at = (
            _parse_wechat_time(data.get("success_time")) or beijing_datetime()
        )
    elif refund_status in {"ABNORMAL", "CLOSED"}:
        restore_transaction_id = (locked_order.extra or {}).get("refund_restore_transaction_id")
        if locked_order.refund_points_transaction_id and not restore_transaction_id:
            restore_transaction = await change_user_points(
                db=db,
                user_id=locked_order.user_id,
                amount=locked_order.points_amount,
                transaction_type="recharge_refund_restore",
                remark=f"充值退款失败恢复积分：{locked_order.out_trade_no}",
                auto_commit=False,
            )
            restore_transaction_id = str(restore_transaction.id)
        locked_order.status = "paid"
        locked_order.extra = {
            **(locked_order.extra or {}),
            "refund_failed_reason": data.get("status") or "退款失败",
            "refund_restore_transaction_id": restore_transaction_id,
        }
    await db.commit()
    await db.refresh(locked_order)
    return locked_order


async def mark_recharge_paid(db: AsyncSession, data: dict) -> UserRechargeOrder:
    out_trade_no = data.get("out_trade_no")
    if not out_trade_no:
        raise AppException("支付回调缺少订单号", code=40040, status_code=400)

    result = await db.execute(
        select(UserRechargeOrder)
        .where(UserRechargeOrder.out_trade_no == out_trade_no)
        .with_for_update()
    )
    order = result.scalar_one_or_none()
    if order is None:
        raise AppException("充值订单不存在", code=40440, status_code=404)
    if order.status in {"paid", "refunding", "refunded"}:
        return order

    trade_state = data.get("trade_state")
    if trade_state != "SUCCESS":
        order.extra = {**(order.extra or {}), "wechat_pay_notify": data}
        await db.commit()
        await db.refresh(order)
        return order

    _validate_wechat_paid_transaction(data)
    paid_amount = int((data.get("amount") or {}).get("total") or 0)
    if paid_amount != order.amount_cents:
        raise AppException("支付金额不一致", code=40041, status_code=400)

    transaction = await change_user_points(
        db=db,
        user_id=order.user_id,
        amount=order.points_amount,
        transaction_type="recharge",
        remark=f"微信支付充值：{order.out_trade_no}",
        auto_commit=False,
    )
    order.status = "paid"
    order.transaction_id = data.get("transaction_id")
    order.points_transaction_id = transaction.id
    order.paid_at = _parse_wechat_time(data.get("success_time")) or beijing_datetime()
    order.extra = {**(order.extra or {}), "wechat_pay_notify": data}
    await db.commit()
    await db.refresh(order)
    return order


def _validate_wechat_paid_transaction(data: dict) -> None:
    if settings.wechat_pay_mock_enabled:
        return
    if data.get("appid") != settings.wechat_pay_appid:
        raise AppException("支付回调应用号不一致", code=40042, status_code=400)
    if data.get("mchid") != settings.wechat_pay_mchid:
        raise AppException("支付回调商户号不一致", code=40042, status_code=400)
    if (data.get("amount") or {}).get("currency") != "CNY":
        raise AppException("支付币种不一致", code=40042, status_code=400)


async def refund_recharge_order(
    db: AsyncSession,
    *,
    order_id: UUID,
    reason: Optional[str],
) -> UserRechargeOrder:
    result = await db.execute(
        select(UserRechargeOrder)
        .where(UserRechargeOrder.id == order_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    order = result.scalar_one_or_none()
    if order is None:
        raise AppException("充值订单不存在", code=40440, status_code=404)
    if order.status == "refunded":
        return order
    if order.status != "paid":
        raise AppException("当前充值订单不可退款", code=40042, status_code=400)

    # Keep the balance locked until the refund and points deduction are recorded together.
    await ensure_user_points_enough(db, order.user_id, order.points_amount)

    out_refund_no = order.out_refund_no or _out_refund_no()
    refund_result = await wechat_pay_client.create_refund(
        out_trade_no=order.out_trade_no,
        out_refund_no=out_refund_no,
        reason=reason or "管理员退款",
        amount_cents=order.amount_cents,
    )
    order.out_refund_no = out_refund_no
    order.refund_id = refund_result.get("refund_id") or order.refund_id
    order.extra = {**(order.extra or {}), "wechat_refund_response": refund_result}
    refund_status = refund_result.get("status")
    if refund_status in {"ABNORMAL", "CLOSED"}:
        raise AppException("退款提交失败，请稍后再试", code=50232, status_code=502)

    refund_transaction = await change_user_points(
        db=db,
        user_id=order.user_id,
        amount=-order.points_amount,
        transaction_type="recharge_refund",
        remark=f"充值退款扣回积分：{order.out_trade_no}",
        auto_commit=False,
    )
    order.refund_points_transaction_id = refund_transaction.id

    if refund_status == "SUCCESS":
        order.status = "refunded"
        order.refunded_at = beijing_datetime()
    else:
        order.status = "refunding"

    await db.commit()
    await db.refresh(order)
    return order


def _amount_yuan_to_cents(amount_yuan: Decimal) -> int:
    amount_yuan = amount_yuan.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if amount_yuan <= 0:
        raise AppException("充值金额必须大于 0", code=40044, status_code=400)
    cents = int(amount_yuan * 100)
    if cents % 10 != 0:
        raise AppException("充值金额最小单位为 0.1 元", code=40045, status_code=400)
    return cents


def _amount_cents_to_points(amount_cents: int) -> int:
    return amount_cents // 10


def _out_trade_no() -> str:
    return f"R{beijing_datetime().strftime('%Y%m%d%H%M%S')}{secrets.token_hex(6).upper()}"


def _out_refund_no() -> str:
    return f"RF{beijing_datetime().strftime('%Y%m%d%H%M%S')}{secrets.token_hex(6).upper()}"


def _pending_recharge_expires_before() -> datetime:
    return beijing_datetime() - timedelta(minutes=max(1, settings.wechat_pay_native_expire_minutes))


def _is_pending_recharge_expired(order: UserRechargeOrder) -> bool:
    return order.created_at <= _pending_recharge_expires_before()


def _parse_wechat_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None
