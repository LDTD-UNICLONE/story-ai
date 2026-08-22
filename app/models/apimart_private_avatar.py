import uuid
from typing import Optional

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class ApimartPrivateAvatarAsset(Base, TimestampMixin):
    __tablename__ = "apimart_private_avatar_assets"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "source_fingerprint",
            name="uq_apimart_private_avatar_assets_user_source",
        ),
        CheckConstraint(
            "status IN ('processing', 'ready', 'failed')",
            name="status",
        ),
        Index(
            "ix_apimart_private_avatar_assets_task_status",
            "provider_task_id",
            "status",
        ),
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
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_name: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_task_id: Mapped[str] = mapped_column(String(255), nullable=False)
    provider_asset_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    provider_asset_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'processing'")
    )
    progress_percent: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    extra: Mapped[dict] = mapped_column(
        JSON, nullable=False, server_default=text("'{}'::json")
    )
