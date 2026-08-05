"""create agent controller states

Revision ID: 0031_agent_controller_states
Revises: 0030_core_asset_locks
Create Date: 2026-07-22 21:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0031_agent_controller_states"
down_revision: Union[str, None] = "0030_core_asset_locks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_controller_states",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("production_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="idle", nullable=False),
        sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_result", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('idle', 'queued', 'running', 'failed')",
            name=op.f("ck_agent_controller_states_status"),
        ),
        sa.ForeignKeyConstraint(
            ["production_id"],
            ["agent_productions.id"],
            name=op.f("fk_agent_controller_states_production_id_agent_productions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_controller_states")),
        sa.UniqueConstraint(
            "production_id",
            name="uq_agent_controller_states_production",
        ),
    )
    op.create_index(
        op.f("ix_agent_controller_states_production_id"),
        "agent_controller_states",
        ["production_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_agent_controller_states_status"),
        "agent_controller_states",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_agent_controller_states_status"),
        table_name="agent_controller_states",
    )
    op.drop_index(
        op.f("ix_agent_controller_states_production_id"),
        table_name="agent_controller_states",
    )
    op.drop_table("agent_controller_states")
