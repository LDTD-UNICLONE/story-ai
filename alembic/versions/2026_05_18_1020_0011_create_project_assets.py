"""create project asset tables

Revision ID: 0011_create_project_assets
Revises: 0010_create_project_chapters
Create Date: 2026-05-18 10:20:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0011_create_project_assets"
down_revision: Union[str, None] = "0010_create_project_chapters"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _common_columns() -> list[sa.Column]:
    return [
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_chapter_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("prompt", sa.Text(), nullable=True),
        sa.Column("reference_image", sa.String(length=512), nullable=True),
        sa.Column("source_content", sa.Text(), nullable=True),
        sa.Column("extra", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def _common_constraints(table_name: str) -> list[sa.Constraint]:
    return [
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name=op.f(f"fk_{table_name}_project_id_projects"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f(f"fk_{table_name}_user_id_users"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_chapter_id"], ["project_chapters.id"], name=op.f(f"fk_{table_name}_source_chapter_id_project_chapters"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table_name}")),
    ]


def _create_common_indexes(table_name: str) -> None:
    op.create_index(op.f(f"ix_{table_name}_name"), table_name, ["name"], unique=False)
    op.create_index(op.f(f"ix_{table_name}_project_id"), table_name, ["project_id"], unique=False)
    op.create_index(op.f(f"ix_{table_name}_source_chapter_id"), table_name, ["source_chapter_id"], unique=False)
    op.create_index(op.f(f"ix_{table_name}_user_id"), table_name, ["user_id"], unique=False)


def _drop_common_indexes(table_name: str) -> None:
    op.drop_index(op.f(f"ix_{table_name}_user_id"), table_name=table_name)
    op.drop_index(op.f(f"ix_{table_name}_source_chapter_id"), table_name=table_name)
    op.drop_index(op.f(f"ix_{table_name}_project_id"), table_name=table_name)
    op.drop_index(op.f(f"ix_{table_name}_name"), table_name=table_name)


def upgrade() -> None:
    op.create_table(
        "project_characters",
        *_common_columns(),
        sa.Column("aliases", sa.JSON(), server_default=sa.text("'[]'::json"), nullable=False),
        sa.Column("identity", sa.String(length=128), nullable=True),
        sa.Column("gender", sa.String(length=32), nullable=True),
        sa.Column("age", sa.String(length=64), nullable=True),
        sa.Column("appearance", sa.Text(), nullable=True),
        sa.Column("personality", sa.Text(), nullable=True),
        sa.Column("relationship", sa.Text(), nullable=True),
        sa.Column("costume", sa.Text(), nullable=True),
        *_common_constraints("project_characters"),
    )
    _create_common_indexes("project_characters")

    op.create_table(
        "project_scenes",
        *_common_columns(),
        sa.Column("location", sa.String(length=255), nullable=True),
        sa.Column("time_of_day", sa.String(length=64), nullable=True),
        sa.Column("environment", sa.Text(), nullable=True),
        sa.Column("atmosphere", sa.Text(), nullable=True),
        *_common_constraints("project_scenes"),
    )
    _create_common_indexes("project_scenes")

    op.create_table(
        "project_props",
        *_common_columns(),
        sa.Column("category", sa.String(length=64), nullable=True),
        sa.Column("appearance", sa.Text(), nullable=True),
        sa.Column("function", sa.Text(), nullable=True),
        *_common_constraints("project_props"),
    )
    _create_common_indexes("project_props")


def downgrade() -> None:
    for table_name in ("project_props", "project_scenes", "project_characters"):
        _drop_common_indexes(table_name)
        op.drop_table(table_name)
