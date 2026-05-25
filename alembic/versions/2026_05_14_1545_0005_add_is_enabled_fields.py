"""add is enabled fields

Revision ID: 0005_add_is_enabled_fields
Revises: 0004_create_styles
Create Date: 2026-05-14 15:45:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0005_add_is_enabled_fields"
down_revision: Union[str, None] = "0004_create_styles"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )
    op.add_column(
        "ai_models",
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("ai_models", "is_enabled")
    op.drop_column("users", "is_enabled")
