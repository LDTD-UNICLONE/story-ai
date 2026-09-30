import uuid

from sqlalchemy import ForeignKey, Integer, JSON, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class CanvasGeneration(Base, TimestampMixin):
    __tablename__ = "canvas_generations"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "canvas_id",
            "node_id",
            "idempotency_key",
            name="uq_canvas_generation_request",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    # History survives editor deletion; results never recreate nodes.
    canvas_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    node_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    idempotency_key: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    content_revision: Mapped[int] = mapped_column(Integer)
    task_record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user_task_records.id", ondelete="CASCADE"), unique=True
    )
    snapshot: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
