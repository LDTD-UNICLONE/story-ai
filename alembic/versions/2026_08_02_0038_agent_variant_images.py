"""add independent images for Agent asset variants

Revision ID: 0038_agent_variant_images
Revises: 0037_agent_four_step_workflow
Create Date: 2026-08-02 18:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0038_agent_variant_images"
down_revision: Union[str, None] = "0037_agent_four_step_workflow"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "agent_asset_variants",
        sa.Column("reference_image", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "agent_asset_variants",
        sa.Column(
            "extra",
            postgresql.JSON(astext_type=sa.Text()),
            server_default=sa.text("'{}'::json"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("agent_asset_variants", "extra")
    op.drop_column("agent_asset_variants", "reference_image")
