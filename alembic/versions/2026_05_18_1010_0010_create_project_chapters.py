"""create project chapters table

Revision ID: 0010_create_project_chapters
Revises: 0009_create_projects
Create Date: 2026-05-18 10:10:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0010_create_project_chapters"
down_revision: Union[str, None] = "0009_create_projects"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "project_chapters",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ai_model_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("title", sa.String(length=128), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("processing_prompt", sa.Text(), nullable=True),
        sa.Column("processed_content", sa.Text(), nullable=True),
        sa.Column("process_status", sa.String(length=32), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("extra", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["ai_model_id"], ["ai_models.id"], name=op.f("fk_project_chapters_ai_model_id_ai_models"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name=op.f("fk_project_chapters_project_id_projects"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_project_chapters_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_project_chapters")),
    )
    op.create_index(op.f("ix_project_chapters_ai_model_id"), "project_chapters", ["ai_model_id"], unique=False)
    op.create_index(op.f("ix_project_chapters_process_status"), "project_chapters", ["process_status"], unique=False)
    op.create_index(op.f("ix_project_chapters_project_id"), "project_chapters", ["project_id"], unique=False)
    op.create_index(op.f("ix_project_chapters_user_id"), "project_chapters", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_project_chapters_user_id"), table_name="project_chapters")
    op.drop_index(op.f("ix_project_chapters_project_id"), table_name="project_chapters")
    op.drop_index(op.f("ix_project_chapters_process_status"), table_name="project_chapters")
    op.drop_index(op.f("ix_project_chapters_ai_model_id"), table_name="project_chapters")
    op.drop_table("project_chapters")
