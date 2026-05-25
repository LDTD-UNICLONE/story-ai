"""add ai model point multipliers

Revision ID: 0018_add_ai_model_point_multipliers
Revises: 0017_create_user_recharge_orders
Create Date: 2026-05-25 15:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018_add_ai_model_point_multipliers"
down_revision: Union[str, None] = "0017_create_user_recharge_orders"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ai_models",
        sa.Column(
            "model_multiplier",
            sa.Numeric(10, 4),
            nullable=False,
            server_default=sa.text("1.0000"),
        ),
    )
    op.add_column(
        "ai_models",
        sa.Column(
            "cache_multiplier",
            sa.Numeric(10, 4),
            nullable=False,
            server_default=sa.text("1.0000"),
        ),
    )
    op.add_column(
        "ai_models",
        sa.Column(
            "completion_multiplier",
            sa.Numeric(10, 4),
            nullable=False,
            server_default=sa.text("1.0000"),
        ),
    )
    op.add_column(
        "ai_models",
        sa.Column(
            "platform_multiplier",
            sa.Numeric(10, 4),
            nullable=False,
            server_default=sa.text("1.0000"),
        ),
    )


def downgrade() -> None:
    op.drop_column("ai_models", "platform_multiplier")
    op.drop_column("ai_models", "completion_multiplier")
    op.drop_column("ai_models", "cache_multiplier")
    op.drop_column("ai_models", "model_multiplier")
