"""Read-only provenance for data converted into canvas nodes."""

import uuid

from sqlalchemy import ForeignKey, JSON, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class CanvasImportRecord(Base, TimestampMixin):
    __tablename__ = "canvas_import_records"
    __table_args__ = (UniqueConstraint("project_id", "source_type", "source_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    source_type: Mapped[str] = mapped_column(String(32))
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    data: Mapped[dict] = mapped_column(JSON)
    nodes: Mapped[list] = mapped_column(JSON)
    warnings: Mapped[list] = mapped_column(JSON)
