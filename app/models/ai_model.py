import uuid
from decimal import Decimal
from typing import Optional

from sqlalchemy import Boolean, Index, Integer, JSON, Numeric, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class AiModel(Base, TimestampMixin):
    __tablename__ = "ai_models"
    __table_args__ = (
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
    model_id: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    vendor: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    model_type: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    remark: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    points_cost: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    model_multiplier: Mapped[Decimal] = mapped_column(
        Numeric(10, 4),
        nullable=False,
        default=Decimal("1.0000"),
        server_default=text("1.0000"),
    )
    cache_multiplier: Mapped[Decimal] = mapped_column(
        Numeric(10, 4),
        nullable=False,
        default=Decimal("1.0000"),
        server_default=text("1.0000"),
    )
    completion_multiplier: Mapped[Decimal] = mapped_column(
        Numeric(10, 4),
        nullable=False,
        default=Decimal("1.0000"),
        server_default=text("1.0000"),
    )
    platform_multiplier: Mapped[Decimal] = mapped_column(
        Numeric(10, 4),
        nullable=False,
        default=Decimal("1.0000"),
        server_default=text("1.0000"),
    )
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    is_agent_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    capabilities: Mapped[dict] = mapped_column(
        JSON, nullable=False, server_default=text("'{}'::json")
    )
