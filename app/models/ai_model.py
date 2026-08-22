import uuid
from typing import Optional

from sqlalchemy import Boolean, Index, JSON, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class AiModel(Base, TimestampMixin):
    __tablename__ = "ai_models"
    __table_args__ = (
        UniqueConstraint(
            "vendor",
            "model_id",
            name="uq_ai_models_vendor_model_id",
        ),
        Index(
            "uq_ai_models_agent_default_type",
            "model_type",
            unique=True,
            postgresql_where=text("is_agent_default IS TRUE"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    nickname: Mapped[str] = mapped_column(String(64), nullable=False)
    model_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    vendor: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    model_type: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    remark: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    is_agent_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    configuration: Mapped[dict] = mapped_column(
        JSON, nullable=False, server_default=text("'{}'::json")
    )
