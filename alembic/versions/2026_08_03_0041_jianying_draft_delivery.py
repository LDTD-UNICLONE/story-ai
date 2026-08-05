"""add jianying draft delivery type

Revision ID: 0041_jianying_draft_delivery
Revises: 0040_storyboard_primary_video
Create Date: 2026-08-03 18:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0041_jianying_draft_delivery"
down_revision: Union[str, None] = "0040_storyboard_primary_video"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint(
        op.f("ck_agent_deliveries_delivery_type"),
        "agent_deliveries",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_agent_deliveries_delivery_type"),
        "agent_deliveries",
        "delivery_type IN ('manifest', 'merged_video', 'jianying_draft')",
    )


def downgrade() -> None:
    op.execute("DELETE FROM agent_deliveries WHERE delivery_type = 'jianying_draft'")
    op.drop_constraint(
        op.f("ck_agent_deliveries_delivery_type"),
        "agent_deliveries",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_agent_deliveries_delivery_type"),
        "agent_deliveries",
        "delivery_type IN ('manifest', 'merged_video')",
    )
