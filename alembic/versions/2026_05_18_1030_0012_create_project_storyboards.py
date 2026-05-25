"""create project storyboards table

Revision ID: 0012_create_project_storyboards
Revises: 0011_create_project_assets
Create Date: 2026-05-18 10:30:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0012_create_project_storyboards"
down_revision: Union[str, None] = "0011_create_project_assets"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "project_storyboards",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chapter_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ai_model_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("shot_number", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("title", sa.String(length=128), nullable=False),
        sa.Column("source_content", sa.Text(), nullable=False),
        sa.Column("scene_name", sa.String(length=128), nullable=True),
        sa.Column("characters", sa.JSON(), server_default=sa.text("'[]'::json"), nullable=False),
        sa.Column("props", sa.JSON(), server_default=sa.text("'[]'::json"), nullable=False),
        sa.Column("action", sa.Text(), nullable=True),
        sa.Column("dialogue", sa.Text(), nullable=True),
        sa.Column("emotion", sa.Text(), nullable=True),
        sa.Column("visual_description", sa.Text(), nullable=True),
        sa.Column("image_prompt", sa.Text(), nullable=True),
        sa.Column("video_prompt", sa.Text(), nullable=True),
        sa.Column("duration_suggestion", sa.String(length=64), nullable=True),
        sa.Column("extra", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["ai_model_id"], ["ai_models.id"], name=op.f("fk_project_storyboards_ai_model_id_ai_models"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["chapter_id"], ["project_chapters.id"], name=op.f("fk_project_storyboards_chapter_id_project_chapters"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name=op.f("fk_project_storyboards_project_id_projects"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_project_storyboards_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_project_storyboards")),
    )
    op.create_index(op.f("ix_project_storyboards_ai_model_id"), "project_storyboards", ["ai_model_id"], unique=False)
    op.create_index(op.f("ix_project_storyboards_chapter_id"), "project_storyboards", ["chapter_id"], unique=False)
    op.create_index(op.f("ix_project_storyboards_project_id"), "project_storyboards", ["project_id"], unique=False)
    op.create_index(op.f("ix_project_storyboards_user_id"), "project_storyboards", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_project_storyboards_user_id"), table_name="project_storyboards")
    op.drop_index(op.f("ix_project_storyboards_project_id"), table_name="project_storyboards")
    op.drop_index(op.f("ix_project_storyboards_chapter_id"), table_name="project_storyboards")
    op.drop_index(op.f("ix_project_storyboards_ai_model_id"), table_name="project_storyboards")
    op.drop_table("project_storyboards")
