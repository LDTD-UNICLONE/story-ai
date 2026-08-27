"""add explicit private avatar review opt-in to characters

Revision ID: 0059_character_avatar_opt_in
Revises: 0058_ai_model_vendor_identity
Create Date: 2026-08-24 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0059_character_avatar_opt_in"
down_revision: Union[str, None] = "0058_ai_model_vendor_identity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "project_characters",
        sa.Column(
            "requires_private_avatar_review",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("project_characters", "requires_private_avatar_review")
