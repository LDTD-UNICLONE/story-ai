"""add per-episode storyboard media production requests

Revision ID: 0039_storyboard_media_requests
Revises: 0038_agent_variant_images
Create Date: 2026-08-03 12:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0039_storyboard_media_requests"
down_revision: Union[str, None] = "0038_agent_variant_images"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_storyboard_media_requests",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("production_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chapter_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("media_type", sa.String(length=16), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column(
            "items",
            postgresql.JSON(astext_type=sa.Text()),
            server_default=sa.text("'[]'::json"),
            nullable=False,
        ),
        sa.Column(
            "submitted_points",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column(
            "extra",
            postgresql.JSON(astext_type=sa.Text()),
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
            "media_type IN ('image', 'video')",
            name=op.f("ck_agent_storyboard_media_requests_media_type"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'submitted', 'failed')",
            name=op.f("ck_agent_storyboard_media_requests_status"),
        ),
        sa.ForeignKeyConstraint(["chapter_id"], ["project_chapters.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["production_id"], ["agent_productions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "production_id",
            "media_type",
            "idempotency_key",
            name="uq_agent_storyboard_media_requests_key",
        ),
    )
    op.create_index(
        op.f("ix_agent_storyboard_media_requests_chapter_id"),
        "agent_storyboard_media_requests",
        ["chapter_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_agent_storyboard_media_requests_production_id"),
        "agent_storyboard_media_requests",
        ["production_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_agent_storyboard_media_requests_status"),
        "agent_storyboard_media_requests",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_agent_storyboard_media_requests_user_id"),
        "agent_storyboard_media_requests",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_agent_storyboard_media_requests_user_id"), table_name="agent_storyboard_media_requests")
    op.drop_index(op.f("ix_agent_storyboard_media_requests_status"), table_name="agent_storyboard_media_requests")
    op.drop_index(op.f("ix_agent_storyboard_media_requests_production_id"), table_name="agent_storyboard_media_requests")
    op.drop_index(op.f("ix_agent_storyboard_media_requests_chapter_id"), table_name="agent_storyboard_media_requests")
    op.drop_table("agent_storyboard_media_requests")
