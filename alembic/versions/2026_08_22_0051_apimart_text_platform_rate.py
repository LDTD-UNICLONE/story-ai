"""set APIMart text platform rate to 1.2

Revision ID: 0051_apimart_text_platform_rate
Revises: 0050_unsettled_points_index
Create Date: 2026-08-22 10:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0051_apimart_text_platform_rate"
down_revision: Union[str, None] = "0050_unsettled_points_index"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE ai_models
        SET platform_multiplier = 1.2000
        WHERE vendor = 'apimart'
          AND model_type = 'text'
          AND platform_multiplier = 2.0000
        """
    )


def downgrade() -> None:
    # Platform rates are mutable admin pricing data. Reverting the schema must not
    # overwrite rates that an administrator may have changed after this migration.
    pass
