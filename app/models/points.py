import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class UserPointsTransaction(Base, TimestampMixin):
    __tablename__ = "user_points_transactions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)
    transaction_type: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    remark: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))


class UserRechargeOrder(Base, TimestampMixin):
    __tablename__ = "user_recharge_orders"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    points_transaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_points_transactions.id", ondelete="SET NULL"),
        nullable=True,
    )
    refund_points_transaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_points_transactions.id", ondelete="SET NULL"),
        nullable=True,
    )
    out_trade_no: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    transaction_id: Mapped[Optional[str]] = mapped_column(String(128), unique=True, nullable=True)
    out_refund_no: Mapped[Optional[str]] = mapped_column(String(64), unique=True, nullable=True)
    refund_id: Mapped[Optional[str]] = mapped_column(String(128), unique=True, nullable=True)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    points_amount: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), index=True, nullable=False, server_default=text("'pending'"))
    code_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    description: Mapped[str] = mapped_column(String(128), nullable=False)
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    refunded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, server_default=text("'{}'::json"))

    @property
    def pay_type(self) -> str:
        return "wechat_native"

    @property
    def trade_type(self) -> str:
        return "NATIVE"
