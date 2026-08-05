"""create story bible versions and agent asset candidates

Revision ID: 0029_story_bible_candidates
Revises: 0028_create_agent_production_core
Create Date: 2026-07-22 10:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0029_story_bible_candidates"
down_revision: Union[str, None] = "0028_create_agent_production_core"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "series_bible_versions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("production_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("step_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "status", sa.String(length=32), server_default=sa.text("'draft'"), nullable=False
        ),
        sa.Column("content", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("confirmed_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["confirmed_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["production_id"], ["agent_productions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["step_id"], ["agent_steps.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('draft', 'confirmed', 'superseded')",
            name=op.f("ck_series_bible_versions_status"),
        ),
        sa.UniqueConstraint(
            "production_id",
            "version",
            name="uq_series_bible_versions_production_version",
        ),
    )
    for column in ("project_id", "production_id", "step_id", "status"):
        op.create_index(f"ix_series_bible_versions_{column}", "series_bible_versions", [column])

    op.create_table(
        "agent_asset_candidates",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("production_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("bible_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("asset_type", sa.String(length=32), nullable=False),
        sa.Column("candidate_key", sa.String(length=64), nullable=False),
        sa.Column("canonical_name", sa.String(length=128), nullable=False),
        sa.Column("aliases", sa.JSON(), server_default=sa.text("'[]'::json"), nullable=False),
        sa.Column(
            "source_chapter_ids", sa.JSON(), server_default=sa.text("'[]'::json"), nullable=False
        ),
        sa.Column("confidence", sa.Float(), server_default=sa.text("1"), nullable=False),
        sa.Column("merge_reason", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "review_status", sa.String(length=32), server_default=sa.text("'ready'"), nullable=False
        ),
        sa.Column("content", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False),
        sa.Column("materialized_asset_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lock_version", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["bible_version_id"], ["series_bible_versions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["production_id"], ["agent_productions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "asset_type IN ('character', 'scene', 'prop')",
            name=op.f("ck_agent_asset_candidates_type"),
        ),
        sa.CheckConstraint(
            "review_status IN ('ready', 'needs_review', 'rejected', 'materialized')",
            name=op.f("ck_agent_asset_candidates_review_status"),
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name=op.f("ck_agent_asset_candidates_confidence"),
        ),
        sa.UniqueConstraint(
            "bible_version_id",
            "asset_type",
            "candidate_key",
            name="uq_agent_asset_candidates_bible_type_key",
        ),
    )
    for column in (
        "project_id",
        "production_id",
        "bible_version_id",
        "user_id",
        "asset_type",
        "review_status",
        "materialized_asset_id",
    ):
        op.create_index(f"ix_agent_asset_candidates_{column}", "agent_asset_candidates", [column])


def downgrade() -> None:
    op.drop_table("agent_asset_candidates")
    op.drop_table("series_bible_versions")
