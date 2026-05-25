"""add performance indexes

Revision ID: 0015_add_performance_indexes
Revises: 0014_create_announcements
Create Date: 2026-05-21 15:00:00.000000
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0015_add_performance_indexes"
down_revision: Union[str, None] = "0014_create_announcements"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_user_task_records_user_status",
        "user_task_records",
        ["user_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_user_task_records_user_status_updated_at",
        "user_task_records",
        ["user_id", "status", "updated_at"],
        unique=False,
    )
    op.create_index(
        "ix_user_task_records_user_generation_status",
        "user_task_records",
        ["user_id", "generation_type", "status"],
        unique=False,
    )
    op.create_index(
        "ix_user_task_records_user_created_at",
        "user_task_records",
        ["user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_conversation_messages_conversation_created_at",
        "conversation_messages",
        ["conversation_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_conversations_user_enabled_updated_at",
        "conversations",
        ["user_id", "is_enabled", "updated_at"],
        unique=False,
    )
    op.create_index(
        "ix_project_storyboards_owner_order",
        "project_storyboards",
        ["project_id", "chapter_id", "user_id", "is_enabled", "shot_number", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_project_characters_owner_enabled",
        "project_characters",
        ["project_id", "user_id", "is_enabled"],
        unique=False,
    )
    op.create_index(
        "ix_project_scenes_owner_enabled",
        "project_scenes",
        ["project_id", "user_id", "is_enabled"],
        unique=False,
    )
    op.create_index(
        "ix_project_props_owner_enabled",
        "project_props",
        ["project_id", "user_id", "is_enabled"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_project_props_owner_enabled", table_name="project_props")
    op.drop_index("ix_project_scenes_owner_enabled", table_name="project_scenes")
    op.drop_index("ix_project_characters_owner_enabled", table_name="project_characters")
    op.drop_index("ix_project_storyboards_owner_order", table_name="project_storyboards")
    op.drop_index("ix_conversations_user_enabled_updated_at", table_name="conversations")
    op.drop_index("ix_conversation_messages_conversation_created_at", table_name="conversation_messages")
    op.drop_index("ix_user_task_records_user_created_at", table_name="user_task_records")
    op.drop_index("ix_user_task_records_user_generation_status", table_name="user_task_records")
    op.drop_index("ix_user_task_records_user_status_updated_at", table_name="user_task_records")
    op.drop_index("ix_user_task_records_user_status", table_name="user_task_records")
