import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


AGENT_ASSET_VARIANT_TYPES = {
    "character": frozenset({"costume", "makeup", "age", "injury", "disguise"}),
    "scene": frozenset(
        {"time", "weather", "season", "festival", "damage", "layout", "state"}
    ),
    "prop": frozenset(
        {
            "form",
            "damage",
            "open_state",
            "bloodied",
            "upgrade",
            "ownership",
            "state",
        }
    ),
}


class SeriesBibleVersion(Base, TimestampMixin):
    __tablename__ = "series_bible_versions"
    __table_args__ = (
        UniqueConstraint(
            "production_id",
            "version",
            name="uq_series_bible_versions_production_version",
        ),
        CheckConstraint(
            "status IN ('draft', 'confirmed', 'superseded')",
            name="status",
        ),
    )

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
    production_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_productions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    step_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_steps.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        index=True,
        nullable=False,
        server_default=text("'draft'"),
    )
    content: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        server_default=text("'{}'::json"),
    )
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    confirmed_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentAssetCandidate(Base, TimestampMixin):
    __tablename__ = "agent_asset_candidates"
    __table_args__ = (
        UniqueConstraint(
            "bible_version_id",
            "asset_type",
            "candidate_key",
            name="uq_agent_asset_candidates_bible_type_key",
        ),
        UniqueConstraint(
            "id",
            "project_id",
            "production_id",
            "bible_version_id",
            "user_id",
            "asset_type",
            name="uq_agent_asset_candidates_variant_parent",
        ),
        CheckConstraint(
            "asset_type IN ('character', 'scene', 'prop')",
            name="type",
        ),
        CheckConstraint(
            "review_status IN ('ready', 'needs_review', 'rejected', 'materialized')",
            name="review_status",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="confidence",
        ),
    )

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
    production_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_productions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    bible_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("series_bible_versions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    asset_type: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    candidate_key: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_name: Mapped[str] = mapped_column(String(128), nullable=False)
    aliases: Mapped[list] = mapped_column(JSON, nullable=False, server_default=text("'[]'::json"))
    source_chapter_ids: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        server_default=text("'[]'::json"),
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("1"))
    merge_reason: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    review_status: Mapped[str] = mapped_column(
        String(32),
        index=True,
        nullable=False,
        server_default=text("'ready'"),
    )
    content: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        server_default=text("'{}'::json"),
    )
    materialized_asset_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        index=True,
        nullable=True,
    )
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    @property
    def episode_numbers(self):
        values = (self.content or {}).get("episode_numbers") or []
        return sorted(
            {
                number
                for value in values
                if str(value).isdigit() and (number := int(value)) > 0
            }
        )


class AgentAssetVariant(Base, TimestampMixin):
    __tablename__ = "agent_asset_variants"
    __table_args__ = (
        UniqueConstraint(
            "bible_version_id",
            "base_candidate_id",
            "variant_key",
            name="uq_agent_asset_variants_bible_base_key",
        ),
        CheckConstraint(
            "asset_type IN ('character', 'scene', 'prop')",
            name="type",
        ),
        CheckConstraint(
            "review_status IN ('ready', 'needs_review', 'rejected')",
            name="review_status",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="confidence",
        ),
        CheckConstraint(
            "("
            "asset_type = 'character' AND "
            "variant_type IN ('costume', 'makeup', 'age', 'injury', 'disguise')"
            ") OR ("
            "asset_type = 'scene' AND "
            "variant_type IN ('time', 'weather', 'season', 'festival', "
            "'damage', 'layout', 'state')"
            ") OR ("
            "asset_type = 'prop' AND "
            "variant_type IN ('form', 'damage', 'open_state', 'bloodied', "
            "'upgrade', 'ownership', 'state')"
            ")",
            name="variant_type",
        ),
        ForeignKeyConstraint(
            [
                "base_candidate_id",
                "project_id",
                "production_id",
                "bible_version_id",
                "user_id",
                "asset_type",
            ],
            [
                "agent_asset_candidates.id",
                "agent_asset_candidates.project_id",
                "agent_asset_candidates.production_id",
                "agent_asset_candidates.bible_version_id",
                "agent_asset_candidates.user_id",
                "agent_asset_candidates.asset_type",
            ],
            name="fk_agent_asset_variants_consistent_base",
            ondelete="CASCADE",
        ),
    )

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
    production_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_productions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    bible_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("series_bible_versions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    base_candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_asset_candidates.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    asset_type: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    variant_key: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_name: Mapped[str] = mapped_column(String(128), nullable=False)
    variant_type: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    trigger_reason: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    episode_numbers: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        server_default=text("'[]'::json"),
    )
    source_evidence: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        server_default=text("'[]'::json"),
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("1"))
    review_status: Mapped[str] = mapped_column(
        String(32),
        index=True,
        nullable=False,
        server_default=text("'ready'"),
    )
    content: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        server_default=text("'{}'::json"),
    )
    reference_image: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    extra: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        server_default=text("'{}'::json"),
    )
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
