"""add retry state to private avatar assets

Revision ID: 0060_private_avatar_retry_state
Revises: 0059_character_avatar_opt_in
Create Date: 2026-08-25 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0060_private_avatar_retry_state"
down_revision: Union[str, None] = "0059_character_avatar_opt_in"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "apimart_private_avatar_assets",
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("1"), nullable=False),
    )
    op.add_column(
        "apimart_private_avatar_assets",
        sa.Column("failure_kind", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "apimart_private_avatar_assets",
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.drop_constraint(
        op.f("ck_apimart_private_avatar_assets_status"),
        "apimart_private_avatar_assets",
        type_="check",
    )
    op.execute(
        "UPDATE apimart_private_avatar_assets "
        "SET status = 'retryable_failed', failure_kind = 'legacy_failure', "
        "next_retry_at = now() WHERE status = 'failed'"
    )
    op.create_check_constraint(
        op.f("ck_apimart_private_avatar_assets_status"),
        "apimart_private_avatar_assets",
        "status IN ('processing', 'ready', 'retryable_failed', 'rejected')",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_apimart_private_avatar_assets_status"),
        "apimart_private_avatar_assets",
        type_="check",
    )
    op.execute(
        "UPDATE apimart_private_avatar_assets SET status = 'failed' "
        "WHERE status IN ('retryable_failed', 'rejected')"
    )
    op.create_check_constraint(
        op.f("ck_apimart_private_avatar_assets_status"),
        "apimart_private_avatar_assets",
        "status IN ('processing', 'ready', 'failed')",
    )
    op.drop_column("apimart_private_avatar_assets", "next_retry_at")
    op.drop_column("apimart_private_avatar_assets", "failure_kind")
    op.drop_column("apimart_private_avatar_assets", "attempt_count")
