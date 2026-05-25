"""create conversations

Revision ID: 0006_create_conversations
Revises: 0005_add_is_enabled_fields
Create Date: 2026-05-14 16:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0006_create_conversations"
down_revision: Union[str, None] = "0005_add_is_enabled_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=128), nullable=False),
        sa.Column("conversation_type", sa.String(length=32), nullable=False),
        sa.Column("ai_model_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
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
            name=op.f("fk_conversations_ai_model_id_ai_models"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_conversations_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
    )
    op.create_index(op.f("ix_conversations_ai_model_id"), "conversations", ["ai_model_id"], unique=False)
    op.create_index(
        op.f("ix_conversations_conversation_type"),
        "conversations",
        ["conversation_type"],
        unique=False,
    )
    op.create_index(op.f("ix_conversations_user_id"), "conversations", ["user_id"], unique=False)

    op.create_table(
        "conversation_messages",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("message_type", sa.String(length=32), nullable=False),
        sa.Column("extra", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False),
        sa.Column("ai_model_id", postgresql.UUID(as_uuid=True), nullable=False),
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
            name=op.f("fk_conversation_messages_ai_model_id_ai_models"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_conversation_messages_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_conversation_messages_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversation_messages")),
    )
    op.create_index(
        op.f("ix_conversation_messages_ai_model_id"),
        "conversation_messages",
        ["ai_model_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_conversation_messages_conversation_id"),
        "conversation_messages",
        ["conversation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_conversation_messages_role"),
        "conversation_messages",
        ["role"],
        unique=False,
    )
    op.create_index(
        op.f("ix_conversation_messages_message_type"),
        "conversation_messages",
        ["message_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_conversation_messages_user_id"),
        "conversation_messages",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_conversation_messages_user_id"), table_name="conversation_messages")
    op.drop_index(op.f("ix_conversation_messages_message_type"), table_name="conversation_messages")
    op.drop_index(op.f("ix_conversation_messages_role"), table_name="conversation_messages")
    op.drop_index(op.f("ix_conversation_messages_conversation_id"), table_name="conversation_messages")
    op.drop_index(op.f("ix_conversation_messages_ai_model_id"), table_name="conversation_messages")
    op.drop_table("conversation_messages")
    op.drop_index(op.f("ix_conversations_user_id"), table_name="conversations")
    op.drop_index(op.f("ix_conversations_conversation_type"), table_name="conversations")
    op.drop_index(op.f("ix_conversations_ai_model_id"), table_name="conversations")
    op.drop_table("conversations")
