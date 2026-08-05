"""create fixed four-step Agent workflow states

Revision ID: 0037_agent_four_step_workflow
Revises: 0036_agent_draft_configuration
Create Date: 2026-08-01 17:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0037_agent_four_step_workflow"
down_revision: Union[str, None] = "0036_agent_draft_configuration"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_workflow_step_states",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("production_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scope_type", sa.String(length=16), nullable=False),
        sa.Column("scope_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("step_number", sa.Integer(), nullable=False),
        sa.Column("step_code", sa.String(length=32), nullable=False),
        sa.Column(
            "status",
            sa.String(length=24),
            server_default=sa.text("'not_started'"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "extra",
            postgresql.JSON(astext_type=sa.Text()),
            server_default=sa.text("'{}'::json"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "scope_type IN ('production', 'episode')",
            name="ck_agent_workflow_step_states_scope_type",
        ),
        sa.CheckConstraint(
            "step_number >= 1 AND step_number <= 4",
            name="ck_agent_workflow_step_states_step_number",
        ),
        sa.CheckConstraint(
            "status IN ('not_started', 'processing', 'waiting_review', "
            "'completed', 'failed', 'invalidated')",
            name="ck_agent_workflow_step_states_status",
        ),
        sa.ForeignKeyConstraint(
            ["production_id"],
            ["agent_productions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "production_id",
            "scope_type",
            "scope_id",
            "step_number",
            name="uq_agent_workflow_step_states_scope_step",
        ),
    )
    op.create_index(
        op.f("ix_agent_workflow_step_states_production_id"),
        "agent_workflow_step_states",
        ["production_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_agent_workflow_step_states_status"),
        "agent_workflow_step_states",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_agent_workflow_step_states_status"),
        table_name="agent_workflow_step_states",
    )
    op.drop_index(
        op.f("ix_agent_workflow_step_states_production_id"),
        table_name="agent_workflow_step_states",
    )
    op.drop_table("agent_workflow_step_states")
