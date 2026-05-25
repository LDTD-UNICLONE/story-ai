"""add user points

Revision ID: 0002_add_user_points
Revises: 0001_create_users
Create Date: 2026-05-14 15:15:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0002_add_user_points"
down_revision: Union[str, None] = "0001_create_users"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("points_balance", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.create_table(
        "user_points_transactions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("balance_after", sa.Integer(), nullable=False),
        sa.Column("transaction_type", sa.String(length=32), nullable=False),
        sa.Column("remark", sa.Text(), server_default=sa.text("''"), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_points_transactions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_points_transactions")),
    )
    op.create_index(
        op.f("ix_user_points_transactions_transaction_type"),
        "user_points_transactions",
        ["transaction_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_user_points_transactions_user_id"),
        "user_points_transactions",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_user_points_transactions_user_id"), table_name="user_points_transactions")
    op.drop_index(
        op.f("ix_user_points_transactions_transaction_type"),
        table_name="user_points_transactions",
    )
    op.drop_table("user_points_transactions")
    op.drop_column("users", "points_balance")
