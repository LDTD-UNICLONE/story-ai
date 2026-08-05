import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, JSON, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class AgentWorkflowStepState(Base, TimestampMixin):
    __tablename__ = "agent_workflow_step_states"
    __table_args__ = (
        UniqueConstraint(
            "production_id",
            "scope_type",
            "scope_id",
            "step_number",
            name="uq_agent_workflow_step_states_scope_step",
        ),
        CheckConstraint("scope_type IN ('production', 'episode')", name="scope_type"),
        CheckConstraint("step_number >= 1 AND step_number <= 4", name="step_number"),
        CheckConstraint(
            "status IN ('not_started', 'processing', 'waiting_review', "
            "'completed', 'failed', 'invalidated')",
            name="status",
        ),
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
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    step_number: Mapped[int] = mapped_column(Integer, nullable=False)
    step_code: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24),
        index=True,
        nullable=False,
        server_default=text("'not_started'"),
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    extra: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        server_default=text("'{}'::json"),
    )
