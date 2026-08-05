"""repair task status updated index

Revision ID: 0046_repair_task_status_updated_index
Revises: 0045_validate_variant_constraints
Create Date: 2026-08-06 18:30:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0046_repair_task_status_updated_index"
down_revision: Union[str, None] = "0045_validate_variant_constraints"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS
            ix_user_task_records_user_status_updated_at
            ON user_task_records (user_id, status, updated_at)
            """
        )


def downgrade() -> None:
    # Revision 0015 owns this index. This repair migration must not remove it
    # when rolling back from 0046 to the already-indexed 0045 schema.
    pass
