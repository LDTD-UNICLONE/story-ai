import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class UserTaskRecord(Base, TimestampMixin):
    __tablename__ = "user_task_records"
    __table_args__ = (
        Index("ix_user_task_records_user_status", "user_id", "status"),
        Index(
            "ix_user_task_records_user_status_updated_at",
            "user_id",
            "status",
            "updated_at",
        ),
        Index(
            "ix_user_task_records_user_generation_status",
            "user_id",
            "generation_type",
            "status",
        ),
        Index("ix_user_task_records_user_created_at", "user_id", "created_at"),
        Index("ix_user_task_records_created_id", "created_at", "id"),
        Index(
            "ix_user_task_records_status_created_id",
            "status",
            "created_at",
            "id",
        ),
        Index(
            "ix_user_task_records_business_generation_status_created",
            "business_type",
            "generation_type",
            "status",
            "created_at",
        ),
        Index(
            "ix_user_task_records_provider_task",
            "provider_vendor",
            "provider_task_id",
        ),
        Index(
            "ix_user_task_records_provider_reconcile_due",
            "next_reconcile_at",
            "id",
            postgresql_where=text(
                "provider_task_id IS NOT NULL AND status IN ('pending', 'running')"
            ),
        ),
        Index(
            "ix_user_task_records_success_points_unsettled",
            "updated_at",
            "id",
            postgresql_where=text(
                "status = 'success' AND business_type = 'conversation' "
                "AND extra ->> 'points_settled' = 'false'"
            ),
        ),
    )

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
    ai_model_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ai_models.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    points_transaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_points_transactions.id", ondelete="SET NULL"),
        nullable=True,
    )
    business_type: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    business_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), index=True, nullable=True
    )
    generation_type: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    result: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    points_cost: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    extra: Mapped[dict] = mapped_column(JSON, nullable=False, server_default=text("'{}'::json"))
    provider_vendor: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    provider_task_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    provider_status: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    provider_submitted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_reconcile_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_reconcile_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reconcile_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
