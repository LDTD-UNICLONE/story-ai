"""create announcements

Revision ID: 0014_create_announcements
Revises: 0013_add_storyboard_execution_fields
Create Date: 2026-05-20 10:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0014_create_announcements"
down_revision: Union[str, None] = "0013_add_storyboard_execution_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "announcements",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("title", sa.String(length=128), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("image_url", sa.String(length=512), nullable=True),
        sa.Column("link_url", sa.String(length=512), nullable=True),
        sa.Column("announcement_type", sa.String(length=32), server_default=sa.text("'notice'"), nullable=False),
        sa.Column("display_position", sa.String(length=32), server_default=sa.text("'home'"), nullable=False),
        sa.Column("style_config", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_announcements")),
    )
    op.create_index(op.f("ix_announcements_announcement_type"), "announcements", ["announcement_type"], unique=False)
    op.create_index(op.f("ix_announcements_display_position"), "announcements", ["display_position"], unique=False)
    op.create_index(op.f("ix_announcements_is_enabled"), "announcements", ["is_enabled"], unique=False)
    op.create_index(op.f("ix_announcements_sort_order"), "announcements", ["sort_order"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_announcements_sort_order"), table_name="announcements")
    op.drop_index(op.f("ix_announcements_is_enabled"), table_name="announcements")
    op.drop_index(op.f("ix_announcements_display_position"), table_name="announcements")
    op.drop_index(op.f("ix_announcements_announcement_type"), table_name="announcements")
    op.drop_table("announcements")
