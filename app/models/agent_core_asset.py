import uuid
from typing import Optional

from sqlalchemy import CheckConstraint, ForeignKey, Integer, JSON, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class AgentCoreAssetLock(Base, TimestampMixin):
    __tablename__ = "agent_core_asset_locks"
    __table_args__ = (
        UniqueConstraint(
            "production_id",
            "version",
            name="uq_agent_core_asset_locks_production_version",
        ),
        UniqueConstraint(
            "production_id",
            "idempotency_key",
            name="uq_agent_core_asset_locks_production_idempotency",
        ),
        CheckConstraint(
            "status IN ('active', 'superseded')",
            name="status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    production_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_productions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    bible_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("series_bible_versions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    step_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_steps.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        index=True,
        nullable=False,
        server_default=text("'active'"),
    )
    assets: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        server_default=text("'[]'::json"),
    )
    impact: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        server_default=text("'{}'::json"),
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
