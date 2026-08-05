"""allow unconfigured agent project drafts

Revision ID: 0036_agent_draft_configuration
Revises: 0035_agent_variant_integrity
Create Date: 2026-08-01 10:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0036_agent_draft_configuration"
down_revision: Union[str, None] = "0035_agent_variant_integrity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "projects",
        "style_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    op.alter_column(
        "projects",
        "generation_ratio",
        existing_type=sa.String(length=16),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "projects",
        "generation_ratio",
        existing_type=sa.String(length=16),
        nullable=False,
    )
    op.alter_column(
        "projects",
        "style_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
