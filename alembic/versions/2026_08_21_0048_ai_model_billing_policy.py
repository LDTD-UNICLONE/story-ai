"""add per-model billing policy

Revision ID: 0048_ai_model_billing_policy
Revises: 0047_apimart_private_avatar_assets
Create Date: 2026-08-21 18:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0048_ai_model_billing_policy"
down_revision: Union[str, None] = "0047_apimart_private_avatar_assets"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ai_models",
        sa.Column(
            "billing_policy",
            sa.JSON(),
            server_default=sa.text("'{}'::json"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("ai_models", "billing_policy")
