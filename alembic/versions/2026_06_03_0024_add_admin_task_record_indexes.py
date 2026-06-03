"""add admin task record indexes

Revision ID: 0024_add_admin_task_record_indexes
Revises: 0023_create_project_generated_assets
Create Date: 2026-06-03 16:55:00.000000
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0024_add_admin_task_record_indexes"
down_revision: Union[str, None] = "0023_create_project_generated_assets"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_user_task_records_created_id",
        "user_task_records",
        ["created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_user_task_records_status_created_id",
        "user_task_records",
        ["status", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_user_task_records_business_generation_status_created",
        "user_task_records",
        ["business_type", "generation_type", "status", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_user_task_records_business_generation_status_created", table_name="user_task_records")
    op.drop_index("ix_user_task_records_status_created_id", table_name="user_task_records")
    op.drop_index("ix_user_task_records_created_id", table_name="user_task_records")
