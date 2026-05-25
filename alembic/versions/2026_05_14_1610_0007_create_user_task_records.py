"""create user task records

Revision ID: 0007_create_user_task_records
Revises: 0006_create_conversations
Create Date: 2026-05-14 16:10:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0007_create_user_task_records"
down_revision: Union[str, None] = "0006_create_conversations"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_task_records",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ai_model_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("points_transaction_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("business_type", sa.String(length=32), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("generation_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=128), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("points_cost", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("extra", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False),
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
            ["ai_model_id"],
            ["ai_models.id"],
            name=op.f("fk_user_task_records_ai_model_id_ai_models"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["points_transaction_id"],
            ["user_points_transactions.id"],
            name=op.f("fk_user_task_records_points_transaction_id_user_points_transactions"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_task_records_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_task_records")),
    )
    op.create_index(op.f("ix_user_task_records_ai_model_id"), "user_task_records", ["ai_model_id"])
    op.create_index(op.f("ix_user_task_records_business_id"), "user_task_records", ["business_id"])
    op.create_index(op.f("ix_user_task_records_business_type"), "user_task_records", ["business_type"])
    op.create_index(op.f("ix_user_task_records_generation_type"), "user_task_records", ["generation_type"])
    op.create_index(op.f("ix_user_task_records_status"), "user_task_records", ["status"])
    op.create_index(op.f("ix_user_task_records_user_id"), "user_task_records", ["user_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_user_task_records_user_id"), table_name="user_task_records")
    op.drop_index(op.f("ix_user_task_records_status"), table_name="user_task_records")
    op.drop_index(op.f("ix_user_task_records_generation_type"), table_name="user_task_records")
    op.drop_index(op.f("ix_user_task_records_business_type"), table_name="user_task_records")
    op.drop_index(op.f("ix_user_task_records_business_id"), table_name="user_task_records")
    op.drop_index(op.f("ix_user_task_records_ai_model_id"), table_name="user_task_records")
    op.drop_table("user_task_records")
