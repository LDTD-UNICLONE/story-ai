"""create user recharge orders

Revision ID: 0017_create_user_recharge_orders
Revises: 0016_transfer_project_owner
Create Date: 2026-05-24 10:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017_create_user_recharge_orders"
down_revision: Union[str, None] = "0016_transfer_project_owner"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_recharge_orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("points_transaction_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("refund_points_transaction_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("out_trade_no", sa.String(length=64), nullable=False),
        sa.Column("transaction_id", sa.String(length=128), nullable=True),
        sa.Column("out_refund_no", sa.String(length=64), nullable=True),
        sa.Column("refund_id", sa.String(length=128), nullable=True),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("points_amount", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("code_url", sa.Text(), nullable=True),
        sa.Column("description", sa.String(length=128), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("extra", postgresql.JSON(astext_type=sa.Text()), server_default=sa.text("'{}'::json"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["points_transaction_id"], ["user_points_transactions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["refund_points_transaction_id"], ["user_points_transactions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_user_recharge_orders_out_trade_no"), "user_recharge_orders", ["out_trade_no"], unique=True)
    op.create_index(op.f("ix_user_recharge_orders_status"), "user_recharge_orders", ["status"], unique=False)
    op.create_index(op.f("ix_user_recharge_orders_user_id"), "user_recharge_orders", ["user_id"], unique=False)
    op.create_index("ix_user_recharge_orders_user_created_at", "user_recharge_orders", ["user_id", "created_at"], unique=False)
    op.create_unique_constraint("uq_user_recharge_orders_transaction_id", "user_recharge_orders", ["transaction_id"])
    op.create_unique_constraint("uq_user_recharge_orders_out_refund_no", "user_recharge_orders", ["out_refund_no"])
    op.create_unique_constraint("uq_user_recharge_orders_refund_id", "user_recharge_orders", ["refund_id"])


def downgrade() -> None:
    op.drop_constraint("uq_user_recharge_orders_refund_id", "user_recharge_orders", type_="unique")
    op.drop_constraint("uq_user_recharge_orders_out_refund_no", "user_recharge_orders", type_="unique")
    op.drop_constraint("uq_user_recharge_orders_transaction_id", "user_recharge_orders", type_="unique")
    op.drop_index("ix_user_recharge_orders_user_created_at", table_name="user_recharge_orders")
    op.drop_index(op.f("ix_user_recharge_orders_user_id"), table_name="user_recharge_orders")
    op.drop_index(op.f("ix_user_recharge_orders_status"), table_name="user_recharge_orders")
    op.drop_index(op.f("ix_user_recharge_orders_out_trade_no"), table_name="user_recharge_orders")
    op.drop_table("user_recharge_orders")
