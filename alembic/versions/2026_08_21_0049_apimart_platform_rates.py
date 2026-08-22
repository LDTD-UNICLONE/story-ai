"""set APIMart default platform rates

Revision ID: 0049_apimart_platform_rates
Revises: 0048_ai_model_billing_policy
Create Date: 2026-08-21 19:30:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0049_apimart_platform_rates"
down_revision: Union[str, None] = "0048_ai_model_billing_policy"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE ai_models
        SET platform_multiplier = CASE
            WHEN model_type = 'text' THEN 2.0000
            WHEN model_type = 'video' THEN 1.2000
            ELSE platform_multiplier
        END
        WHERE vendor = 'apimart'
          AND model_type IN ('text', 'video')
          AND platform_multiplier = 1.0000
        """
    )


def downgrade() -> None:
    # Platform rates are mutable admin pricing data. Reverting the schema must not
    # overwrite rates that an administrator may have changed after this migration.
    pass
