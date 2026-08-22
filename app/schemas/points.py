from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator
from app.schemas.base import SchemaBaseModel


class PointsTransactionOut(SchemaBaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    amount: int
    balance_after: int
    transaction_type: str
    remark: str
    created_at: datetime


class PointsTransactionListOut(SchemaBaseModel):
    items: List[PointsTransactionOut]
    total: int
    page: int
    page_size: int


class PointsBalanceOut(SchemaBaseModel):
    points_balance: int


class PointsRecordUserOut(SchemaBaseModel):
    id: UUID
    account: str
    nickname: str
    avatar: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None


class AdminPointsRecordOut(SchemaBaseModel):
    id: UUID
    user_id: UUID
    user: PointsRecordUserOut
    amount: int
    balance_after: int
    transaction_type: str
    remark: str
    created_at: datetime


class AdminPointsRecordListOut(SchemaBaseModel):
    items: List[AdminPointsRecordOut]
    total: int
    page: int
    page_size: int


class AdminPointsAdjustRequest(SchemaBaseModel):
    amount: int = Field(..., description="正数增加积分，负数扣减积分")
    remark: str = Field(..., min_length=1, max_length=500)

    @field_validator("amount")
    @classmethod
    def reject_zero_amount(cls, value: int) -> int:
        if value == 0:
            raise ValueError("积分变动数量不能为 0")
        return value

    @field_validator("remark")
    @classmethod
    def normalize_remark(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("积分调整原因不能为空")
        return normalized


class PointsConsumeRequest(SchemaBaseModel):
    amount: int = Field(..., gt=0)
    remark: Optional[str] = Field(default=None, max_length=500)


class RechargeCreateRequest(SchemaBaseModel):
    amount_yuan: Decimal = Field(..., gt=0, description="充值金额，单位元；1 元 = 10 积分")


class RechargeOrderOut(SchemaBaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    points_transaction_id: Optional[UUID] = None
    refund_points_transaction_id: Optional[UUID] = None
    out_trade_no: str
    transaction_id: Optional[str] = None
    out_refund_no: Optional[str] = None
    refund_id: Optional[str] = None
    amount_cents: int
    points_amount: int
    pay_type: str = "wechat_native"
    trade_type: str = "NATIVE"
    status: str
    code_url: Optional[str] = None
    description: str
    paid_at: Optional[datetime] = None
    refunded_at: Optional[datetime] = None
    extra: Dict[str, Any]
    created_at: datetime
    updated_at: datetime


class AdminRechargeOrderOut(RechargeOrderOut):
    user: PointsRecordUserOut


class RechargeOrderListOut(SchemaBaseModel):
    items: List[RechargeOrderOut]
    total: int
    page: int
    page_size: int


class AdminRechargeOrderListOut(SchemaBaseModel):
    items: List[AdminRechargeOrderOut]
    total: int
    page: int
    page_size: int


class RechargeRefundRequest(SchemaBaseModel):
    reason: Optional[str] = Field(default="管理员退款", max_length=80)
