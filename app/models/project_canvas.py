import uuid

from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class ProjectCanvas(Base, TimestampMixin):
    __tablename__ = "project_canvases"
    __table_args__ = (CheckConstraint("revision >= 1", name="revision_positive"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(128))
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    viewport: Mapped[dict] = mapped_column(JSON, default=lambda: {"x": 0, "y": 0, "zoom": 1})


class CanvasNode(Base):
    __tablename__ = "canvas_nodes"
    __table_args__ = (
        CheckConstraint("kind IN ('text','image','video','group')", name="kind"),
        CheckConstraint("width > 0 AND height > 0", name="positive_size"),
        CheckConstraint("content_revision >= 1", name="content_revision_positive"),
        ForeignKeyConstraint(
            ["canvas_id", "parent_id"],
            ["canvas_nodes.canvas_id", "canvas_nodes.id"],
            deferrable=True,
            initially="DEFERRED",
            name="fk_canvas_nodes_parent",
        ),
    )

    canvas_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_canvases.id", ondelete="CASCADE"), primary_key=True
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(128))
    x: Mapped[float] = mapped_column(Float)
    y: Mapped[float] = mapped_column(Float)
    width: Mapped[float] = mapped_column(Float)
    height: Mapped[float] = mapped_column(Float)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    media_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_media.id"), nullable=True
    )
    content: Mapped[dict] = mapped_column(JSON)
    content_revision: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    import_record_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("canvas_import_records.id", ondelete="SET NULL"), nullable=True
    )
    latest_generation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    selected_generation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class CanvasEdge(Base):
    __tablename__ = "canvas_edges"
    __table_args__ = (
        ForeignKeyConstraint(
            ["canvas_id", "source_id"],
            ["canvas_nodes.canvas_id", "canvas_nodes.id"],
            ondelete="CASCADE",
            name="fk_canvas_edges_source",
        ),
        ForeignKeyConstraint(
            ["canvas_id", "target_id"],
            ["canvas_nodes.canvas_id", "canvas_nodes.id"],
            ondelete="CASCADE",
            name="fk_canvas_edges_target",
        ),
        UniqueConstraint(
            "canvas_id",
            "target_id",
            "input",
            "position",
            name="uq_canvas_edges_input_position",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint("source_id <> target_id", name="not_self"),
        CheckConstraint("input IN ('reference','first_frame','last_frame','text')", name="input"),
        CheckConstraint("position >= 0", name="position_nonnegative"),
    )

    canvas_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_canvases.id", ondelete="CASCADE"), primary_key=True
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    input: Mapped[str] = mapped_column(String(16))
    text_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    text_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    position: Mapped[int] = mapped_column(Integer)
    media_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_media.id"), nullable=True
    )
