import uuid
from typing import Optional

from sqlalchemy import CheckConstraint, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class AgentStoryboardMediaRequest(Base, TimestampMixin):
    __tablename__ = "agent_storyboard_media_requests"
    __table_args__ = (
        UniqueConstraint(
            "production_id",
            "media_type",
            "idempotency_key",
            name="uq_agent_storyboard_media_requests_key",
        ),
        CheckConstraint("media_type IN ('image', 'video')", name="media_type"),
        CheckConstraint("status IN ('pending', 'submitted', 'failed')", name="status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    production_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_productions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    chapter_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("project_chapters.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    media_type: Mapped[str] = mapped_column(String(16), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        index=True,
        nullable=False,
        server_default=text("'pending'"),
    )
    items: Mapped[list] = mapped_column(JSON, nullable=False, server_default=text("'[]'::json"))
    submitted_points: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
    error_summary: Mapped[Optional[str]] = mapped_column(Text)
    extra: Mapped[dict] = mapped_column(JSON, nullable=False, server_default=text("'{}'::json"))
