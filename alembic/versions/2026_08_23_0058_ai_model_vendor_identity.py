"""scope AI model identity to its provider

Revision ID: 0058_ai_model_vendor_identity
Revises: 0057_apimart_video_billing
Create Date: 2026-08-23 01:30:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0058_ai_model_vendor_identity"
down_revision: Union[str, None] = "0057_apimart_video_billing"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("ix_ai_models_model_id", table_name="ai_models")
    op.create_index(
        "ix_ai_models_model_id",
        "ai_models",
        ["model_id"],
        unique=False,
    )
    op.create_unique_constraint(
        "uq_ai_models_vendor_model_id",
        "ai_models",
        ["vendor", "model_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_ai_models_vendor_model_id",
        "ai_models",
        type_="unique",
    )
    op.drop_index("ix_ai_models_model_id", table_name="ai_models")
    op.create_index(
        "ix_ai_models_model_id",
        "ai_models",
        ["model_id"],
        unique=True,
    )
