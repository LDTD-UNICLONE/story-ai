"""add text asset variants for agent script processing

Revision ID: 0034_agent_asset_variants
Revises: 0033_agent_entry_defaults
Create Date: 2026-07-28 10:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0034_agent_asset_variants"
down_revision: Union[str, None] = "0033_agent_entry_defaults"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_asset_variants",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("production_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("bible_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("base_candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("asset_type", sa.String(length=32), nullable=False),
        sa.Column("variant_key", sa.String(length=64), nullable=False),
        sa.Column("canonical_name", sa.String(length=128), nullable=False),
        sa.Column("variant_type", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("trigger_reason", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "episode_numbers",
            sa.JSON(),
            server_default=sa.text("'[]'::json"),
            nullable=False,
        ),
        sa.Column(
            "source_evidence",
            sa.JSON(),
            server_default=sa.text("'[]'::json"),
            nullable=False,
        ),
        sa.Column("confidence", sa.Float(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "review_status",
            sa.String(length=32),
            server_default=sa.text("'ready'"),
            nullable=False,
        ),
        sa.Column(
            "content",
            sa.JSON(),
            server_default=sa.text("'{}'::json"),
            nullable=False,
        ),
        sa.Column("lock_version", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "asset_type IN ('character', 'scene', 'prop')",
            name=op.f("ck_agent_asset_variants_type"),
        ),
        sa.CheckConstraint(
            "review_status IN ('ready', 'needs_review', 'rejected')",
            name=op.f("ck_agent_asset_variants_review_status"),
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name=op.f("ck_agent_asset_variants_confidence"),
        ),
        sa.ForeignKeyConstraint(
            ["base_candidate_id"],
            ["agent_asset_candidates.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["bible_version_id"],
            ["series_bible_versions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["production_id"],
            ["agent_productions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "bible_version_id",
            "base_candidate_id",
            "variant_key",
            name="uq_agent_asset_variants_bible_base_key",
        ),
    )
    for column in (
        "project_id",
        "production_id",
        "bible_version_id",
        "base_candidate_id",
        "user_id",
        "asset_type",
        "review_status",
    ):
        op.create_index(
            op.f(f"ix_agent_asset_variants_{column}"),
            "agent_asset_variants",
            [column],
            unique=False,
        )


def downgrade() -> None:
    op.drop_table("agent_asset_variants")
