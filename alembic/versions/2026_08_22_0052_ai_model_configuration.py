"""add unified ai model configuration

Revision ID: 0052_ai_model_configuration
Revises: 0051_apimart_text_platform_rate
Create Date: 2026-08-22 15:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0052_ai_model_configuration"
down_revision: Union[str, None] = "0051_apimart_text_platform_rate"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ai_models",
        sa.Column(
            "configuration",
            sa.JSON(),
            server_default=sa.text("'{}'::json"),
            nullable=False,
        ),
    )
    op.execute(
        """
        UPDATE ai_models
        SET configuration = json_build_object(
            'version', 1,
            'request', json_build_object(
                'capabilities', COALESCE(capabilities, '{}'::json)
            ),
            'billing', json_build_object(
                'base_points', points_cost,
                'multipliers', json_build_object(
                    'model', model_multiplier::text,
                    'cache', cache_multiplier::text,
                    'completion', completion_multiplier::text,
                    'platform', platform_multiplier::text
                ),
                'policy', COALESCE(billing_policy, '{}'::json)
            ),
            'operations', json_build_object(
                'status', 'active',
                'maintenance_message', NULL
            )
        )
        """
    )


def downgrade() -> None:
    op.drop_column("ai_models", "configuration")
