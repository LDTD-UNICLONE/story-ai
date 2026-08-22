"""index successful conversation tasks awaiting points settlement

Revision ID: 0050_unsettled_points_index
Revises: 0049_apimart_platform_rates
Create Date: 2026-08-21 21:20:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0050_unsettled_points_index"
down_revision: Union[str, None] = "0049_apimart_platform_rates"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS
            ix_user_task_records_success_points_unsettled
            ON user_task_records (updated_at, id)
            WHERE status = 'success'
              AND business_type = 'conversation'
              AND extra ->> 'points_settled' = 'false'
            """
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            """
            DROP INDEX CONCURRENTLY IF EXISTS
            ix_user_task_records_success_points_unsettled
            """
        )
