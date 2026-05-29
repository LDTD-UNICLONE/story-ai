"""create materials table

Revision ID: 0021_create_materials
Revises: 0020_add_storyboard_prompt_fields
Create Date: 2026-05-29 18:30:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021_create_materials"
down_revision: Union[str, None] = "0020_add_storyboard_prompt_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "materials",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "tags",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("image_url", sa.String(length=1024), nullable=False),
        sa.Column("image_object_key", sa.String(length=512), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        sa.Column("size", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("image_object_key"),
    )
    op.create_index(op.f("ix_materials_category"), "materials", ["category"], unique=False)
    op.create_index(op.f("ix_materials_name"), "materials", ["name"], unique=False)
    op.create_index(
        "ix_materials_is_enabled_sort_order",
        "materials",
        ["is_enabled", "sort_order"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_materials_is_enabled_sort_order", table_name="materials")
    op.drop_index(op.f("ix_materials_name"), table_name="materials")
    op.drop_index(op.f("ix_materials_category"), table_name="materials")
    op.drop_table("materials")
