import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class SeedanceImage(Base, TimestampMixin):
    __tablename__ = "seedance_images"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "provider_scope", "sha256", name="uq_seedance_images_user_scope_hash"
        ),
        CheckConstraint(
            "status IN ('pending', 'submitting', 'processing', 'ready', 'failed', 'uncertain')",
            name="status",
        ),
        Index("ix_seedance_images_review_due", "next_poll_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    provider_scope: Mapped[str] = mapped_column(String(64), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    upload: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'pending'")
    )
    provider_task_id: Mapped[Optional[str]] = mapped_column(String(255))
    asset_url: Mapped[Optional[str]] = mapped_column(String(512))
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    poll_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    next_poll_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    review_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
