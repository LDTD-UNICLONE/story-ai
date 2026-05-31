"""create project generated assets

Revision ID: 0023_create_project_generated_assets
Revises: 0022_tighten_storyboard_fields
Create Date: 2026-05-31 15:50:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0023_create_project_generated_assets"
down_revision: Union[str, None] = "0022_tighten_storyboard_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "project_generated_assets",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chapter_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_record_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("ai_model_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("media_type", sa.String(length=16), nullable=False),
        sa.Column("result_url", sa.String(length=1024), nullable=True),
        sa.Column("result_urls", sa.JSON(), server_default=sa.text("'[]'::json"), nullable=False),
        sa.Column("last_frame_url", sa.String(length=1024), nullable=True),
        sa.Column("prompt", sa.Text(), nullable=True),
        sa.Column("generation_mode", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), server_default=sa.text("'success'"), nullable=False),
        sa.Column("is_selected", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("extra", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["ai_model_id"], ["ai_models.id"], name=op.f("fk_project_generated_assets_ai_model_id_ai_models"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["chapter_id"], ["project_chapters.id"], name=op.f("fk_project_generated_assets_chapter_id_project_chapters"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name=op.f("fk_project_generated_assets_project_id_projects"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_record_id"], ["user_task_records.id"], name=op.f("fk_project_generated_assets_task_record_id_user_task_records"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_project_generated_assets_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_project_generated_assets")),
    )
    op.create_index(op.f("ix_project_generated_assets_ai_model_id"), "project_generated_assets", ["ai_model_id"], unique=False)
    op.create_index(op.f("ix_project_generated_assets_chapter_id"), "project_generated_assets", ["chapter_id"], unique=False)
    op.create_index(op.f("ix_project_generated_assets_media_type"), "project_generated_assets", ["media_type"], unique=False)
    op.create_index(op.f("ix_project_generated_assets_project_id"), "project_generated_assets", ["project_id"], unique=False)
    op.create_index(op.f("ix_project_generated_assets_status"), "project_generated_assets", ["status"], unique=False)
    op.create_index(op.f("ix_project_generated_assets_target_id"), "project_generated_assets", ["target_id"], unique=False)
    op.create_index(op.f("ix_project_generated_assets_target_type"), "project_generated_assets", ["target_type"], unique=False)
    op.create_index(op.f("ix_project_generated_assets_task_record_id"), "project_generated_assets", ["task_record_id"], unique=False)
    op.create_index(op.f("ix_project_generated_assets_user_id"), "project_generated_assets", ["user_id"], unique=False)
    op.create_index(
        "ix_project_generated_assets_target_media_selected",
        "project_generated_assets",
        ["project_id", "user_id", "target_type", "target_id", "media_type", "is_selected"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_project_generated_assets_target_media_selected", table_name="project_generated_assets")
    op.drop_index(op.f("ix_project_generated_assets_user_id"), table_name="project_generated_assets")
    op.drop_index(op.f("ix_project_generated_assets_task_record_id"), table_name="project_generated_assets")
    op.drop_index(op.f("ix_project_generated_assets_target_type"), table_name="project_generated_assets")
    op.drop_index(op.f("ix_project_generated_assets_target_id"), table_name="project_generated_assets")
    op.drop_index(op.f("ix_project_generated_assets_status"), table_name="project_generated_assets")
    op.drop_index(op.f("ix_project_generated_assets_project_id"), table_name="project_generated_assets")
    op.drop_index(op.f("ix_project_generated_assets_media_type"), table_name="project_generated_assets")
    op.drop_index(op.f("ix_project_generated_assets_chapter_id"), table_name="project_generated_assets")
    op.drop_index(op.f("ix_project_generated_assets_ai_model_id"), table_name="project_generated_assets")
    op.drop_table("project_generated_assets")
