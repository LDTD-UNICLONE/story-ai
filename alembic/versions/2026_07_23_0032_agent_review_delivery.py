"""create agent review and delivery tables

Revision ID: 0032_agent_review_delivery
Revises: 0031_agent_controller_states
Create Date: 2026-07-23 10:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0032_agent_review_delivery"
down_revision: Union[str, None] = "0031_agent_controller_states"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_review_issues",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("production_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chapter_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("storyboard_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("media_type", sa.String(length=16), nullable=True),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'open'"), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("resolved_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lock_version", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("extra", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("severity IN ('info', 'warning', 'blocking')", name=op.f("ck_agent_review_issues_severity")),
        sa.CheckConstraint("status IN ('open', 'resolved', 'dismissed')", name=op.f("ck_agent_review_issues_status")),
        sa.ForeignKeyConstraint(["chapter_id"], ["project_chapters.id"], name=op.f("fk_agent_review_issues_chapter_id_project_chapters"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["production_id"], ["agent_productions.id"], name=op.f("fk_agent_review_issues_production_id_agent_productions"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["resolved_by"], ["users.id"], name=op.f("fk_agent_review_issues_resolved_by_users"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["storyboard_id"], ["project_storyboards.id"], name=op.f("fk_agent_review_issues_storyboard_id_project_storyboards"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_agent_review_issues_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_review_issues")),
    )
    for column in ("production_id", "chapter_id", "storyboard_id", "user_id", "category", "severity", "status"):
        op.create_index(op.f(f"ix_agent_review_issues_{column}"), "agent_review_issues", [column], unique=False)

    op.create_table(
        "agent_episode_reviews",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("production_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chapter_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("media_snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("approved_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lock_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("extra", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('approved', 'invalidated')", name=op.f("ck_agent_episode_reviews_status")),
        sa.ForeignKeyConstraint(["approved_by"], ["users.id"], name=op.f("fk_agent_episode_reviews_approved_by_users"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["chapter_id"], ["project_chapters.id"], name=op.f("fk_agent_episode_reviews_chapter_id_project_chapters"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["production_id"], ["agent_productions.id"], name=op.f("fk_agent_episode_reviews_production_id_agent_productions"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_agent_episode_reviews_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_episode_reviews")),
        sa.UniqueConstraint("production_id", "chapter_id", name="uq_agent_episode_reviews_scope"),
    )
    for column in ("production_id", "chapter_id", "user_id", "status"):
        op.create_index(op.f(f"ix_agent_episode_reviews_{column}"), "agent_episode_reviews", [column], unique=False)

    op.create_table(
        "agent_media_regenerations",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("production_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("media_type", sa.String(length=16), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("items", sa.JSON(), server_default=sa.text("'[]'::json"), nullable=False),
        sa.Column("estimated_points", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("submitted_points", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("extra", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("media_type IN ('image', 'video')", name=op.f("ck_agent_media_regenerations_media_type")),
        sa.CheckConstraint("status IN ('pending', 'submitted', 'failed')", name=op.f("ck_agent_media_regenerations_status")),
        sa.ForeignKeyConstraint(["production_id"], ["agent_productions.id"], name=op.f("fk_agent_media_regenerations_production_id_agent_productions"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_agent_media_regenerations_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_media_regenerations")),
        sa.UniqueConstraint("production_id", "idempotency_key", name="uq_agent_media_regenerations_key"),
    )
    for column in ("production_id", "user_id", "status"):
        op.create_index(op.f(f"ix_agent_media_regenerations_{column}"), "agent_media_regenerations", [column], unique=False)

    op.create_table(
        "agent_deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("production_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("delivery_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("manifest", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False),
        sa.Column("output_url", sa.String(length=1024), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("extra", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("delivery_type IN ('manifest', 'merged_video')", name=op.f("ck_agent_deliveries_delivery_type")),
        sa.CheckConstraint("status IN ('pending', 'running', 'completed', 'failed')", name=op.f("ck_agent_deliveries_status")),
        sa.ForeignKeyConstraint(["production_id"], ["agent_productions.id"], name=op.f("fk_agent_deliveries_production_id_agent_productions"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name=op.f("fk_agent_deliveries_project_id_projects"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_agent_deliveries_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_deliveries")),
        sa.UniqueConstraint("production_id", "idempotency_key", name="uq_agent_deliveries_key"),
    )
    for column in ("production_id", "project_id", "user_id", "status"):
        op.create_index(op.f(f"ix_agent_deliveries_{column}"), "agent_deliveries", [column], unique=False)


def downgrade() -> None:
    for table in (
        "agent_deliveries",
        "agent_media_regenerations",
        "agent_episode_reviews",
        "agent_review_issues",
    ):
        op.drop_table(table)
