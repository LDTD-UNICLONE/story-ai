import uuid
from typing import Optional

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class UserWork(Base, TimestampMixin):
    __tablename__ = "user_works"
    __table_args__ = (
        Index(
            "ix_user_works_public_rank",
            "visibility",
            "status",
            "is_enabled",
            "like_count",
            "created_at",
        ),
        Index("ix_user_works_owner_created", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    visibility: Mapped[str] = mapped_column(
        String(16), index=True, nullable=False, server_default=text("'public'")
    )
    status: Mapped[str] = mapped_column(
        String(16), index=True, nullable=False, server_default=text("'published'")
    )
    like_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    view_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class UserWorkMedia(Base, TimestampMixin):
    __tablename__ = "user_work_media"
    __table_args__ = (
        Index("ix_user_work_media_work_order", "work_id", "sort_order"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    work_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_works.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    media_type: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    url: Mapped[str] = mapped_column(String(1024), nullable=False)
    object_key: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    thumbnail_object_key: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    width: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    height: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    duration_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


class UserWorkLike(Base, TimestampMixin):
    __tablename__ = "user_work_likes"
    __table_args__ = (UniqueConstraint("work_id", "user_id", name="uq_user_work_likes_work_user"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    work_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_works.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )


class UserWorkUpload(Base, TimestampMixin):
    __tablename__ = "user_work_uploads"
    __table_args__ = (
        Index("ix_user_work_uploads_user_used", "user_id", "is_used"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    media_type: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    url: Mapped[str] = mapped_column(String(1024), nullable=False)
    object_key: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    is_used: Mapped[bool] = mapped_column(
        Boolean, index=True, nullable=False, server_default=text("false")
    )
