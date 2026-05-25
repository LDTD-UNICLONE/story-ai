"""add ai model capabilities

Revision ID: 0008_add_ai_model_capabilities
Revises: 0007_create_user_task_records
Create Date: 2026-05-15 02:10:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0008_add_ai_model_capabilities"
down_revision: Union[str, None] = "0007_create_user_task_records"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ai_models",
        sa.Column("capabilities", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("ai_models", "capabilities")
