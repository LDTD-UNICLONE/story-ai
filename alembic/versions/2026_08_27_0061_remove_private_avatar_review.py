"""remove private avatar review feature

Revision ID: 0061_remove_private_avatar_review
Revises: 0060_private_avatar_retry_state
Create Date: 2026-08-27 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0061_remove_private_avatar_review"
down_revision: Union[str, None] = "0060_private_avatar_retry_state"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.drop_table("apimart_private_avatar_assets")
    op.drop_column("project_characters", "requires_private_avatar_review")


def downgrade() -> None:
    op.add_column(
        "project_characters",
        sa.Column(
            "requires_private_avatar_review",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.create_table(
        "apimart_private_avatar_assets",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("source_name", sa.String(length=128), nullable=False),
        sa.Column("provider_task_id", sa.String(length=255), nullable=False),
        sa.Column("provider_asset_id", sa.String(length=255), nullable=True),
        sa.Column("provider_asset_url", sa.String(length=512), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'processing'"),
            nullable=False,
        ),
        sa.Column("progress_percent", sa.Integer(), nullable=True),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column("failure_kind", sa.String(length=64), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "extra",
            sa.JSON(),
            server_default=sa.text("'{}'::json"),
            nullable=False,
        ),
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
            "status IN ('processing', 'ready', 'retryable_failed', 'rejected')",
            name=op.f("ck_apimart_private_avatar_assets_status"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_apimart_private_avatar_assets_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_apimart_private_avatar_assets")),
        sa.UniqueConstraint(
            "user_id",
            "source_fingerprint",
            name="uq_apimart_private_avatar_assets_user_source",
        ),
    )
    op.create_index(
        op.f("ix_apimart_private_avatar_assets_user_id"),
        "apimart_private_avatar_assets",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_apimart_private_avatar_assets_task_status",
        "apimart_private_avatar_assets",
        ["provider_task_id", "status"],
        unique=False,
    )
