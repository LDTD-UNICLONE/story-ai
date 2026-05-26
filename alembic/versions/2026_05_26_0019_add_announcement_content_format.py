"""add announcement content format

Revision ID: 0019_add_announcement_content_format
Revises: 0018_add_ai_model_point_multipliers
Create Date: 2026-05-26 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019_add_announcement_content_format"
down_revision: Union[str, None] = "0018_add_ai_model_point_multipliers"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "announcements",
        sa.Column(
            "content_format",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'plain'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("announcements", "content_format")
