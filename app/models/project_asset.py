import uuid
from typing import Optional

from sqlalchemy import Boolean, ForeignKey, JSON, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship as orm_relationship

from app.db.base import Base, TimestampMixin
from app.models.project import Project
from app.models.project_chapter import ProjectChapter


class ProjectCharacter(Base, TimestampMixin):
    __tablename__ = "project_characters"

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
    source_chapter_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("project_chapters.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    aliases: Mapped[list] = mapped_column(JSON, nullable=False, server_default=text("'[]'::json"))
    identity: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    gender: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    age: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    appearance: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    personality: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    relationship: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    costume: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reference_image: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    source_content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    extra: Mapped[dict] = mapped_column(JSON, nullable=False, server_default=text("'{}'::json"))
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    project: Mapped[Project] = orm_relationship()
    source_chapter: Mapped[Optional[ProjectChapter]] = orm_relationship()


class ProjectScene(Base, TimestampMixin):
    __tablename__ = "project_scenes"

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
    source_chapter_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("project_chapters.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    location: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    time_of_day: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    environment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    atmosphere: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reference_image: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    source_content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    extra: Mapped[dict] = mapped_column(JSON, nullable=False, server_default=text("'{}'::json"))
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    project: Mapped[Project] = orm_relationship()
    source_chapter: Mapped[Optional[ProjectChapter]] = orm_relationship()


class ProjectProp(Base, TimestampMixin):
    __tablename__ = "project_props"

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
    source_chapter_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("project_chapters.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    category: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    appearance: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    function: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reference_image: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    source_content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    extra: Mapped[dict] = mapped_column(JSON, nullable=False, server_default=text("'{}'::json"))
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    project: Mapped[Project] = orm_relationship()
    source_chapter: Mapped[Optional[ProjectChapter]] = orm_relationship()
