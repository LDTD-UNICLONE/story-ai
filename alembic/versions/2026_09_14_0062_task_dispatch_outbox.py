"""persist task dispatch in the business transaction

Revision ID: 0062_task_dispatch_outbox
Revises: 0061_remove_private_avatar_review
Create Date: 2026-09-14 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0062_task_dispatch_outbox"
down_revision = "0061_remove_private_avatar_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_dispatch_outbox",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("task_name", sa.String(128), nullable=False),
        sa.Column("args", sa.JSON(), nullable=False),
        sa.Column("queue", sa.String(64), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_dispatch_outbox")),
    )
    op.create_index(
        "ix_task_dispatch_outbox_next_attempt_at", "task_dispatch_outbox", ["next_attempt_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_task_dispatch_outbox_next_attempt_at", table_name="task_dispatch_outbox")
    op.drop_table("task_dispatch_outbox")
