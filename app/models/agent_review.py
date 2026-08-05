import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
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


class AgentReviewIssue(Base, TimestampMixin):
    __tablename__ = "agent_review_issues"
    __table_args__ = (
        CheckConstraint("severity IN ('info', 'warning', 'blocking')", name="severity"),
        CheckConstraint("status IN ('open', 'resolved', 'dismissed')", name="status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    production_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_productions.id", ondelete="CASCADE"), index=True
    )
    chapter_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_chapters.id", ondelete="CASCADE"), index=True
    )
    storyboard_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_storyboards.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    media_type: Mapped[Optional[str]] = mapped_column(String(16))
    category: Mapped[str] = mapped_column(String(32), index=True)
    severity: Mapped[str] = mapped_column(String(16), index=True)
    status: Mapped[str] = mapped_column(
        String(16), index=True, nullable=False, server_default=text("'open'")
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    resolution_note: Mapped[Optional[str]] = mapped_column(Text)
    resolved_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    extra: Mapped[dict] = mapped_column(JSON, nullable=False, server_default=text("'{}'::json"))


class AgentEpisodeReview(Base, TimestampMixin):
    __tablename__ = "agent_episode_reviews"
    __table_args__ = (
        UniqueConstraint("production_id", "chapter_id", name="uq_agent_episode_reviews_scope"),
        CheckConstraint("status IN ('approved', 'invalidated')", name="status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    production_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_productions.id", ondelete="CASCADE"), index=True
    )
    chapter_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_chapters.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    media_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    approved_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    extra: Mapped[dict] = mapped_column(JSON, nullable=False, server_default=text("'{}'::json"))


class AgentMediaRegeneration(Base, TimestampMixin):
    __tablename__ = "agent_media_regenerations"
    __table_args__ = (
        UniqueConstraint(
            "production_id", "idempotency_key", name="uq_agent_media_regenerations_key"
        ),
        CheckConstraint("media_type IN ('image', 'video')", name="media_type"),
        CheckConstraint("status IN ('pending', 'submitted', 'failed')", name="status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    production_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_productions.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    media_type: Mapped[str] = mapped_column(String(16), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), index=True, nullable=False, server_default=text("'pending'")
    )
    items: Mapped[list] = mapped_column(JSON, nullable=False, server_default=text("'[]'::json"))
    estimated_points: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    submitted_points: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    error_summary: Mapped[Optional[str]] = mapped_column(Text)
    extra: Mapped[dict] = mapped_column(JSON, nullable=False, server_default=text("'{}'::json"))


class AgentDelivery(Base, TimestampMixin):
    __tablename__ = "agent_deliveries"
    __table_args__ = (
        UniqueConstraint("production_id", "idempotency_key", name="uq_agent_deliveries_key"),
        CheckConstraint(
            "delivery_type IN ('manifest', 'merged_video', 'jianying_draft')",
            name="delivery_type",
        ),
        CheckConstraint("status IN ('pending', 'running', 'completed', 'failed')", name="status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    production_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_productions.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    delivery_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), index=True, nullable=False, server_default=text("'pending'")
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    manifest: Mapped[dict] = mapped_column(JSON, nullable=False, server_default=text("'{}'::json"))
    output_url: Mapped[Optional[str]] = mapped_column(String(1024))
    error_summary: Mapped[Optional[str]] = mapped_column(Text)
    lease_token: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    extra: Mapped[dict] = mapped_column(JSON, nullable=False, server_default=text("'{}'::json"))
