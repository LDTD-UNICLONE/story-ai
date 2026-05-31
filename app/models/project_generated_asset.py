import uuid
from typing import Optional

from sqlalchemy import Boolean, ForeignKey, JSON, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.models.ai_model import AiModel
from app.models.project import Project
from app.models.project_chapter import ProjectChapter
from app.models.task_record import UserTaskRecord


class ProjectGeneratedAsset(Base, TimestampMixin):
    __tablename__ = "project_generated_assets"

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
    chapter_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("project_chapters.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    task_record_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_task_records.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    ai_model_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ai_models.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    target_type: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True, nullable=False)
    media_type: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    result_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    result_urls: Mapped[list] = mapped_column(JSON, nullable=False, server_default=text("'[]'::json"))
    last_frame_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    generation_mode: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True, nullable=False, server_default=text("'success'"))
    is_selected: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    extra: Mapped[dict] = mapped_column(JSON, nullable=False, server_default=text("'{}'::json"))
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    project: Mapped[Project] = relationship()
    chapter: Mapped[Optional[ProjectChapter]] = relationship()
    task_record: Mapped[Optional[UserTaskRecord]] = relationship()
    ai_model: Mapped[Optional[AiModel]] = relationship()
