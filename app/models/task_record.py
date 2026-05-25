import uuid
from typing import Optional

from sqlalchemy import ForeignKey, Integer, JSON, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class UserTaskRecord(Base, TimestampMixin):
    __tablename__ = "user_task_records"

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
    business_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), index=True, nullable=True)
    generation_type: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    result: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    points_cost: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    extra: Mapped[dict] = mapped_column(JSON, nullable=False, server_default=text("'{}'::json"))
