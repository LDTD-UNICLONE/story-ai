from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin_user
from app.core.responses import success
from app.db.session import get_db
from app.models.user import User
from app.services.admin_statistics import (
    get_model_type_usage_stats,
    get_points_consumption_stats,
    get_recharge_stats,
    get_task_result_stats,
    get_user_growth_stats,
)

router = APIRouter(prefix="/admin/statistics")


@router.get("/user-growth")
async def admin_user_growth_statistics(
    start_date: Optional[date] = Query(default=None),
    end_date: Optional[date] = Query(default=None),
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    data = await get_user_growth_stats(db, start_date=start_date, end_date=end_date)
    return success(data=data.model_dump(mode="json"))


@router.get("/model-type-usage")
async def admin_model_type_usage_statistics(
    start_date: Optional[date] = Query(default=None),
    end_date: Optional[date] = Query(default=None),
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    data = await get_model_type_usage_stats(db, start_date=start_date, end_date=end_date)
    return success(data=data.model_dump(mode="json"))


@router.get("/points-consumption")
async def admin_points_consumption_statistics(
    start_date: Optional[date] = Query(default=None),
    end_date: Optional[date] = Query(default=None),
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    data = await get_points_consumption_stats(db, start_date=start_date, end_date=end_date)
    return success(data=data.model_dump(mode="json"))


@router.get("/recharges")
async def admin_recharge_statistics(
    start_date: Optional[date] = Query(default=None),
    end_date: Optional[date] = Query(default=None),
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    data = await get_recharge_stats(db, start_date=start_date, end_date=end_date)
    return success(data=data.model_dump(mode="json"))


@router.get("/task-results")
async def admin_task_result_statistics(
    start_date: Optional[date] = Query(default=None),
    end_date: Optional[date] = Query(default=None),
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    data = await get_task_result_stats(db, start_date=start_date, end_date=end_date)
    return success(data=data.model_dump(mode="json"))
