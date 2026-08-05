"""add agent project kind and default models

Revision ID: 0033_agent_entry_defaults
Revises: 0032_agent_review_delivery
Create Date: 2026-07-24 15:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0033_agent_entry_defaults"
down_revision: Union[str, None] = "0032_agent_review_delivery"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column(
            "project_kind",
            sa.String(length=16),
            server_default=sa.text("'standard'"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_projects_project_kind",
        "projects",
        "project_kind IN ('standard', 'agent')",
    )
    op.create_index("ix_projects_project_kind", "projects", ["project_kind"], unique=False)

    op.add_column(
        "ai_models",
        sa.Column(
            "is_agent_default",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.create_index(
        "uq_ai_models_agent_default_type",
        "ai_models",
        ["model_type"],
        unique=True,
        postgresql_where=sa.text("is_agent_default IS TRUE"),
    )


def downgrade() -> None:
    op.drop_index("uq_ai_models_agent_default_type", table_name="ai_models")
    op.drop_column("ai_models", "is_agent_default")
    op.drop_index("ix_projects_project_kind", table_name="projects")
    op.drop_constraint("ck_projects_project_kind", "projects", type_="check")
    op.drop_column("projects", "project_kind")
