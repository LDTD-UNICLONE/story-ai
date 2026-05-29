from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional, Tuple

from sqlalchemy import Date, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import AppException
from app.core.timezone import BEIJING_TZ, beijing_datetime
from app.models.ai_model import AiModel
from app.models.points import UserPointsTransaction, UserRechargeOrder
from app.models.task_record import UserTaskRecord
from app.models.user import User
from app.schemas.admin_statistics import (
    ModelTypeUsageStatItem,
    ModelTypeUsageStatsOut,
    PointsConsumptionStatItem,
    PointsConsumptionStatsOut,
    RechargeStatItem,
    RechargeStatsOut,
    StatisticsDateRangeOut,
    TaskResultStatItem,
    TaskResultStatsOut,
    UserGrowthStatItem,
    UserGrowthStatsOut,
)

DEFAULT_STATISTICS_DAYS = 7
MAX_STATISTICS_DAYS = 366
MODEL_TYPES = ("text", "image", "video")


async def get_user_growth_stats(
    db: AsyncSession,
    *,
    start_date: Optional[date],
    end_date: Optional[date],
) -> UserGrowthStatsOut:
    resolved_start, resolved_end = _resolve_date_range(start_date, end_date)
    day_expr = _beijing_day(User.created_at).label("day")
    result = await db.execute(
        select(day_expr, func.count(User.id))
        .where(*_datetime_range_conditions(User.created_at, resolved_start, resolved_end))
        .group_by(day_expr)
    )
    counts = {_coerce_date(day): int(total or 0) for day, total in result.all()}
    return UserGrowthStatsOut(
        date_range=_date_range_out(resolved_start, resolved_end),
        items=[
            UserGrowthStatItem(date=day, new_users=counts.get(day, 0))
            for day in _date_series(resolved_start, resolved_end)
        ],
    )


async def get_model_type_usage_stats(
    db: AsyncSession,
    *,
    start_date: Optional[date],
    end_date: Optional[date],
) -> ModelTypeUsageStatsOut:
    resolved_start, resolved_end = _resolve_date_range(start_date, end_date)
    day_expr = _beijing_day(UserTaskRecord.created_at).label("day")
    result = await db.execute(
        select(day_expr, AiModel.model_type, func.count(UserTaskRecord.id))
        .join(AiModel, AiModel.id == UserTaskRecord.ai_model_id)
        .where(
            *_datetime_range_conditions(UserTaskRecord.created_at, resolved_start, resolved_end),
            AiModel.model_type.in_(MODEL_TYPES),
        )
        .group_by(day_expr, AiModel.model_type)
    )
    counts: Dict[date, Dict[str, int]] = {}
    for day, model_type, total in result.all():
        day_counts = counts.setdefault(_coerce_date(day), {item: 0 for item in MODEL_TYPES})
        if model_type in MODEL_TYPES:
            day_counts[model_type] = int(total or 0)

    items: List[ModelTypeUsageStatItem] = []
    for day in _date_series(resolved_start, resolved_end):
        day_counts = counts.get(day, {item: 0 for item in MODEL_TYPES})
        text_count = day_counts["text"]
        image_count = day_counts["image"]
        video_count = day_counts["video"]
        items.append(
            ModelTypeUsageStatItem(
                date=day,
                text_count=text_count,
                image_count=image_count,
                video_count=video_count,
                total_count=text_count + image_count + video_count,
            )
        )
    return ModelTypeUsageStatsOut(
        date_range=_date_range_out(resolved_start, resolved_end),
        items=items,
    )


async def get_points_consumption_stats(
    db: AsyncSession,
    *,
    start_date: Optional[date],
    end_date: Optional[date],
) -> PointsConsumptionStatsOut:
    resolved_start, resolved_end = _resolve_date_range(start_date, end_date)
    day_expr = _beijing_day(UserPointsTransaction.created_at).label("day")
    consumed_expr = func.coalesce(func.sum(UserPointsTransaction.amount * -1), 0)
    result = await db.execute(
        select(day_expr, consumed_expr)
        .where(
            *_datetime_range_conditions(UserPointsTransaction.created_at, resolved_start, resolved_end),
            UserPointsTransaction.transaction_type == "consume",
            UserPointsTransaction.amount < 0,
        )
        .group_by(day_expr)
    )
    counts = {_coerce_date(day): int(total or 0) for day, total in result.all()}
    return PointsConsumptionStatsOut(
        date_range=_date_range_out(resolved_start, resolved_end),
        items=[
            PointsConsumptionStatItem(date=day, consumed_points=counts.get(day, 0))
            for day in _date_series(resolved_start, resolved_end)
        ],
    )


async def get_recharge_stats(
    db: AsyncSession,
    *,
    start_date: Optional[date],
    end_date: Optional[date],
) -> RechargeStatsOut:
    resolved_start, resolved_end = _resolve_date_range(start_date, end_date)
    day_expr = _beijing_day(UserRechargeOrder.paid_at).label("day")
    result = await db.execute(
        select(
            day_expr,
            func.coalesce(func.sum(UserRechargeOrder.points_amount), 0),
            func.coalesce(func.sum(UserRechargeOrder.amount_cents), 0),
            func.count(UserRechargeOrder.id),
        )
        .where(
            *_datetime_range_conditions(UserRechargeOrder.paid_at, resolved_start, resolved_end),
            UserRechargeOrder.paid_at.is_not(None),
            UserRechargeOrder.status.in_(("paid", "refunding", "refunded")),
        )
        .group_by(day_expr)
    )
    counts = {
        _coerce_date(day): {
            "points": int(points or 0),
            "amount_cents": int(amount_cents or 0),
            "order_count": int(order_count or 0),
        }
        for day, points, amount_cents, order_count in result.all()
    }
    items: List[RechargeStatItem] = []
    for day in _date_series(resolved_start, resolved_end):
        value = counts.get(day, {"points": 0, "amount_cents": 0, "order_count": 0})
        items.append(
            RechargeStatItem(
                date=day,
                recharge_points=value["points"],
                recharge_amount_cents=value["amount_cents"],
                recharge_amount_yuan=_format_cents_yuan(value["amount_cents"]),
                order_count=value["order_count"],
            )
        )
    return RechargeStatsOut(date_range=_date_range_out(resolved_start, resolved_end), items=items)


async def get_task_result_stats(
    db: AsyncSession,
    *,
    start_date: Optional[date],
    end_date: Optional[date],
) -> TaskResultStatsOut:
    resolved_start, resolved_end = _resolve_date_range(start_date, end_date)
    day_expr = _beijing_day(UserTaskRecord.updated_at).label("day")
    result = await db.execute(
        select(day_expr, UserTaskRecord.status, func.count(UserTaskRecord.id))
        .where(
            *_datetime_range_conditions(UserTaskRecord.updated_at, resolved_start, resolved_end),
            UserTaskRecord.status.in_(("success", "failed")),
        )
        .group_by(day_expr, UserTaskRecord.status)
    )
    counts: Dict[date, Dict[str, int]] = {}
    for day, status, total in result.all():
        day_counts = counts.setdefault(_coerce_date(day), {"success": 0, "failed": 0})
        if status in day_counts:
            day_counts[status] = int(total or 0)

    items: List[TaskResultStatItem] = []
    for day in _date_series(resolved_start, resolved_end):
        day_counts = counts.get(day, {"success": 0, "failed": 0})
        success_count = day_counts["success"]
        failed_count = day_counts["failed"]
        items.append(
            TaskResultStatItem(
                date=day,
                success_count=success_count,
                failed_count=failed_count,
                total_count=success_count + failed_count,
            )
        )
    return TaskResultStatsOut(date_range=_date_range_out(resolved_start, resolved_end), items=items)


def _resolve_date_range(
    start_date: Optional[date],
    end_date: Optional[date],
) -> Tuple[date, date]:
    resolved_end = end_date or beijing_datetime().date()
    resolved_start = start_date or (resolved_end - timedelta(days=DEFAULT_STATISTICS_DAYS - 1))
    if resolved_start > resolved_end:
        raise AppException("开始日期不能晚于结束日期", code=40060, status_code=400)
    if (resolved_end - resolved_start).days + 1 > MAX_STATISTICS_DAYS:
        raise AppException("统计时间范围不能超过 366 天", code=40061, status_code=400)
    return resolved_start, resolved_end


def _datetime_range_conditions(column, start_date: date, end_date: date) -> Tuple[object, object]:
    start_at = datetime.combine(start_date, time.min, tzinfo=BEIJING_TZ)
    end_at = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=BEIJING_TZ)
    return column >= start_at, column < end_at


def _beijing_day(column):
    return cast(func.timezone(settings.timezone, column), Date)


def _date_range_out(start_date: date, end_date: date) -> StatisticsDateRangeOut:
    return StatisticsDateRangeOut(
        start_date=start_date,
        end_date=end_date,
        timezone=settings.timezone,
    )


def _date_series(start_date: date, end_date: date) -> List[date]:
    return [start_date + timedelta(days=offset) for offset in range((end_date - start_date).days + 1)]


def _coerce_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    return value


def _format_cents_yuan(amount_cents: int) -> str:
    return f"{amount_cents / 100:.2f}"
