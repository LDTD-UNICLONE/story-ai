"""create styles table

Revision ID: 0004_create_styles
Revises: 0003_create_ai_models
Create Date: 2026-05-14 15:35:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0004_create_styles"
down_revision: Union[str, None] = "0003_create_ai_models"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "styles",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("cover", sa.String(length=512), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("version", sa.String(length=32), server_default=sa.text("'v1'"), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_styles")),
    )
    op.create_index(op.f("ix_styles_name"), "styles", ["name"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_styles_name"), table_name="styles")
    op.drop_table("styles")
