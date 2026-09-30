import uuid

from sqlalchemy import Boolean, ForeignKey, JSON, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class ProjectMedia(Base, TimestampMixin):
    """Immutable project image versions; deleting a node never deletes its file."""

    __tablename__ = "project_media"
    __table_args__ = (UniqueConstraint("project_id", "source_key", name="uq_project_media_source"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    source_key: Mapped[str] = mapped_column(String(160))
    source_type: Mapped[str] = mapped_column(String(32))
    source_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    media_type: Mapped[str] = mapped_column(String(16), default="image", server_default="image")
    source_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    upload: Mapped[dict] = mapped_column(JSON)
