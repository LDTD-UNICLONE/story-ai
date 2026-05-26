import uuid
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, JSON, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class Announcement(Base, TimestampMixin):
    __tablename__ = "announcements"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_format: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'plain'"))
    image_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    link_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    announcement_type: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'notice'"))
    display_position: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'home'"))
    style_config: Mapped[dict] = mapped_column(JSON, nullable=False, server_default=text("'{}'::json"))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    start_at: Mapped[Optional[DateTime]] = mapped_column(DateTime(timezone=True), nullable=True)
    end_at: Mapped[Optional[DateTime]] = mapped_column(DateTime(timezone=True), nullable=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
