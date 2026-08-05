"""create versioned core asset locks

Revision ID: 0030_core_asset_locks
Revises: 0029_story_bible_candidates
Create Date: 2026-07-22 16:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0030_core_asset_locks"
down_revision: Union[str, None] = "0029_story_bible_candidates"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_core_asset_locks",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("production_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("bible_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("step_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column("assets", sa.JSON(), server_default=sa.text("'[]'::json"), nullable=False),
        sa.Column("impact", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('active', 'superseded')",
            name=op.f("ck_agent_core_asset_locks_status"),
        ),
        sa.ForeignKeyConstraint(
            ["bible_version_id"],
            ["series_bible_versions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["production_id"],
            ["agent_productions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["step_id"], ["agent_steps.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "production_id",
            "version",
            name="uq_agent_core_asset_locks_production_version",
        ),
        sa.UniqueConstraint(
            "production_id",
            "idempotency_key",
            name="uq_agent_core_asset_locks_production_idempotency",
        ),
    )
    for column in (
        "project_id",
        "production_id",
        "bible_version_id",
        "step_id",
        "status",
    ):
        op.create_index(f"ix_agent_core_asset_locks_{column}", "agent_core_asset_locks", [column])


def downgrade() -> None:
    op.drop_table("agent_core_asset_locks")
