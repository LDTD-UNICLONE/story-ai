"""validate agent asset variant constraints

Revision ID: 0045_validate_variant_constraints
Revises: 0044_provider_task_tracking
Create Date: 2026-08-06 16:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0045_validate_variant_constraints"
down_revision: Union[str, None] = "0044_provider_task_tracking"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE agent_asset_variants
        VALIDATE CONSTRAINT ck_agent_asset_variants_variant_type
        """
    )
    op.execute(
        """
        ALTER TABLE agent_asset_variants
        VALIDATE CONSTRAINT fk_agent_asset_variants_consistent_base
        """
    )


def downgrade() -> None:
    # PostgreSQL cannot mark an already validated constraint as NOT VALID
    # without dropping and recreating it. Validation is intentionally retained.
    pass
