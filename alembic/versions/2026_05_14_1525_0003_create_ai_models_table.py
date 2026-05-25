"""create ai models table

Revision ID: 0003_create_ai_models
Revises: 0002_add_user_points
Create Date: 2026-05-14 15:25:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0003_create_ai_models"
down_revision: Union[str, None] = "0002_add_user_points"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ai_models",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("nickname", sa.String(length=64), nullable=False),
        sa.Column("model_id", sa.String(length=128), nullable=False),
        sa.Column("vendor", sa.String(length=64), nullable=False),
        sa.Column("model_type", sa.String(length=64), nullable=False),
        sa.Column("remark", sa.Text(), nullable=True),
        sa.Column("points_cost", sa.Integer(), server_default=sa.text("0"), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_models")),
    )
    op.create_index(op.f("ix_ai_models_model_id"), "ai_models", ["model_id"], unique=True)
    op.create_index(op.f("ix_ai_models_model_type"), "ai_models", ["model_type"], unique=False)
    op.create_index(op.f("ix_ai_models_vendor"), "ai_models", ["vendor"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_ai_models_vendor"), table_name="ai_models")
    op.drop_index(op.f("ix_ai_models_model_type"), table_name="ai_models")
    op.drop_index(op.f("ix_ai_models_model_id"), table_name="ai_models")
    op.drop_table("ai_models")
