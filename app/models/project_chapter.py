import uuid
from typing import Optional

from sqlalchemy import Boolean, ForeignKey, Integer, JSON, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.models.ai_model import AiModel
from app.models.project import Project


class ProjectChapter(Base, TimestampMixin):
    __tablename__ = "project_chapters"

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
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    ai_model_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ai_models.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    processing_prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    processed_content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    process_status: Mapped[str] = mapped_column(String(32), index=True, nullable=False, server_default=text("'draft'"))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    extra: Mapped[dict] = mapped_column(JSON, nullable=False, server_default=text("'{}'::json"))
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    project: Mapped[Project] = relationship()
    ai_model: Mapped[Optional[AiModel]] = relationship()
