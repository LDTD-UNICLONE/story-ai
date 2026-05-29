from datetime import date
from typing import List

from app.schemas.base import SchemaBaseModel


class StatisticsDateRangeOut(SchemaBaseModel):
    start_date: date
    end_date: date
    timezone: str


class UserGrowthStatItem(SchemaBaseModel):
    date: date
    new_users: int


class UserGrowthStatsOut(SchemaBaseModel):
    date_range: StatisticsDateRangeOut
    items: List[UserGrowthStatItem]


class ModelTypeUsageStatItem(SchemaBaseModel):
    date: date
    text_count: int
    image_count: int
    video_count: int
    total_count: int


class ModelTypeUsageStatsOut(SchemaBaseModel):
    date_range: StatisticsDateRangeOut
    items: List[ModelTypeUsageStatItem]


class PointsConsumptionStatItem(SchemaBaseModel):
    date: date
    consumed_points: int


class PointsConsumptionStatsOut(SchemaBaseModel):
    date_range: StatisticsDateRangeOut
    items: List[PointsConsumptionStatItem]


class RechargeStatItem(SchemaBaseModel):
    date: date
    recharge_points: int
    recharge_amount_cents: int
    recharge_amount_yuan: str
    order_count: int


class RechargeStatsOut(SchemaBaseModel):
    date_range: StatisticsDateRangeOut
    items: List[RechargeStatItem]


class TaskResultStatItem(SchemaBaseModel):
    date: date
    success_count: int
    failed_count: int
    total_count: int


class TaskResultStatsOut(SchemaBaseModel):
    date_range: StatisticsDateRangeOut
    items: List[TaskResultStatItem]
