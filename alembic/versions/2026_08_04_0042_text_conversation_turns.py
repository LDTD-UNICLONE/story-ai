"""add reliable text conversation turns

Revision ID: 0042_text_conversation_turns
Revises: 0041_jianying_draft_delivery
Create Date: 2026-08-04 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0042_text_conversation_turns"
down_revision: Union[str, None] = "0041_jianying_draft_delivery"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "conversation_messages",
        sa.Column("turn_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "conversation_messages",
        sa.Column("sequence_no", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "conversation_messages",
        sa.Column("status", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "conversation_messages",
        sa.Column("client_message_id", sa.String(length=128), nullable=True),
    )
    op.create_index(
        op.f("ix_conversation_messages_turn_id"),
        "conversation_messages",
        ["turn_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_conversation_messages_status"),
        "conversation_messages",
        ["status"],
        unique=False,
    )
    op.create_check_constraint(
        op.f("ck_conversation_messages_status"),
        "conversation_messages",
        "status IS NULL OR status IN ('pending', 'running', 'success', 'failed')",
    )
    op.create_index(
        "uq_conversation_messages_conversation_sequence",
        "conversation_messages",
        ["conversation_id", "sequence_no"],
        unique=True,
        postgresql_where=sa.text("sequence_no IS NOT NULL"),
    )
    op.create_index(
        "uq_conversation_messages_conversation_client_message",
        "conversation_messages",
        ["conversation_id", "client_message_id"],
        unique=True,
        postgresql_where=sa.text("client_message_id IS NOT NULL AND role = 'user'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_conversation_messages_conversation_client_message",
        table_name="conversation_messages",
    )
    op.drop_index(
        "uq_conversation_messages_conversation_sequence",
        table_name="conversation_messages",
    )
    op.drop_constraint(
        op.f("ck_conversation_messages_status"),
        "conversation_messages",
        type_="check",
    )
    op.drop_index(op.f("ix_conversation_messages_status"), table_name="conversation_messages")
    op.drop_index(op.f("ix_conversation_messages_turn_id"), table_name="conversation_messages")
    op.drop_column("conversation_messages", "client_message_id")
    op.drop_column("conversation_messages", "status")
    op.drop_column("conversation_messages", "sequence_no")
    op.drop_column("conversation_messages", "turn_id")
