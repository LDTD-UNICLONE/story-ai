import uuid
from typing import Optional

from sqlalchemy import Boolean, ForeignKey, Integer, JSON, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.models.ai_model import AiModel
from app.models.project import Project
from app.models.project_chapter import ProjectChapter


class ProjectStoryboard(Base, TimestampMixin):
    __tablename__ = "project_storyboards"

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
    ai_model_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ai_models.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    shot_number: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    source_content: Mapped[str] = mapped_column(Text, nullable=False)
    scene_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    scene_time: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    scene_state: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    shot_size: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    camera_angle: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    camera_movement: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    screen_execution: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    characters: Mapped[list] = mapped_column(JSON, nullable=False, server_default=text("'[]'::json"))
    props: Mapped[list] = mapped_column(JSON, nullable=False, server_default=text("'[]'::json"))
    action: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    character_action: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    character_expression: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    dialogue: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sound_effect: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    atmosphere: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    emotion: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    visual_description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    image_prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    video_prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    duration_suggestion: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    production_focus: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    negative_prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ending_frame: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    extra: Mapped[dict] = mapped_column(JSON, nullable=False, server_default=text("'{}'::json"))
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    project: Mapped[Project] = relationship()
    chapter: Mapped[ProjectChapter] = relationship()
    ai_model: Mapped[Optional[AiModel]] = relationship()

    @property
    def core_action(self) -> Optional[str]:
        return self.action

    @property
    def storyboard_image_prompt(self) -> Optional[str]:
        return self.image_prompt

    @property
    def event_goal(self) -> Optional[str]:
        extra = self.extra or {}
        raw_item = extra.get("raw_item") if isinstance(extra.get("raw_item"), dict) else {}
        return extra.get("event_goal") or raw_item.get("event_goal")

    @property
    def production_focus_base(self) -> Optional[str]:
        extra = self.extra or {}
        raw_item = extra.get("raw_item") if isinstance(extra.get("raw_item"), dict) else {}
        return extra.get("production_focus_base") or raw_item.get("production_focus_base")

    @property
    def negative_prompt_base(self) -> Optional[str]:
        extra = self.extra or {}
        raw_item = extra.get("raw_item") if isinstance(extra.get("raw_item"), dict) else {}
        return extra.get("negative_prompt_base") or raw_item.get("negative_prompt_base")

    @property
    def split_reason(self) -> Optional[str]:
        extra = self.extra or {}
        raw_item = extra.get("raw_item") if isinstance(extra.get("raw_item"), dict) else {}
        return extra.get("split_reason") or raw_item.get("split_reason")
