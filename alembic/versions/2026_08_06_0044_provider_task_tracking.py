"""add provider task tracking fields

Revision ID: 0044_provider_task_tracking
Revises: 0043_user_token_version
Create Date: 2026-08-06 10:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0044_provider_task_tracking"
down_revision: Union[str, None] = "0043_user_token_version"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "user_task_records",
        sa.Column("provider_vendor", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "user_task_records",
        sa.Column("provider_task_id", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "user_task_records",
        sa.Column("provider_status", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "user_task_records",
        sa.Column("provider_submitted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "user_task_records",
        sa.Column("last_reconcile_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "user_task_records",
        sa.Column("next_reconcile_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "user_task_records",
        sa.Column(
            "reconcile_attempts",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )

    op.execute(
        """
        UPDATE user_task_records AS task
        SET
            provider_task_id = COALESCE(
                NULLIF(task.extra ->> 'task_id', ''),
                NULLIF(task.extra ->> 'provider_task_id', ''),
                NULLIF(task.extra #>> '{model_result_extra,task_id}', ''),
                NULLIF(task.extra #>> '{assistant_message_extra,task_id}', ''),
                NULLIF(task.extra #>> '{last_provider_task_status,task_id}', ''),
                NULLIF(task.extra #>> '{last_provider_task_status,taskId}', '')
            ),
            provider_status = COALESCE(
                NULLIF(task.extra #>> '{last_provider_task_status,task_status}', ''),
                NULLIF(task.extra #>> '{model_result_extra,task_status}', ''),
                NULLIF(task.extra #>> '{assistant_message_extra,task_status}', ''),
                NULLIF(task.extra #>> '{model_result_extra,platform_task_status}', ''),
                NULLIF(task.extra #>> '{assistant_message_extra,platform_task_status}', '')
            ),
            provider_submitted_at = task.updated_at,
            next_reconcile_at = CASE
                WHEN task.status IN ('pending', 'running') THEN task.updated_at
                ELSE NULL
            END
        WHERE COALESCE(
                NULLIF(task.extra ->> 'task_id', ''),
                NULLIF(task.extra ->> 'provider_task_id', ''),
                NULLIF(task.extra #>> '{model_result_extra,task_id}', ''),
                NULLIF(task.extra #>> '{assistant_message_extra,task_id}', ''),
                NULLIF(task.extra #>> '{last_provider_task_status,task_id}', ''),
                NULLIF(task.extra #>> '{last_provider_task_status,taskId}', '')
              ) IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE user_task_records AS task
        SET provider_vendor = model.vendor
        FROM ai_models AS model
        WHERE task.ai_model_id = model.id
          AND task.provider_task_id IS NOT NULL
        """
    )

    op.create_index(
        "ix_user_task_records_provider_task",
        "user_task_records",
        ["provider_vendor", "provider_task_id"],
        unique=False,
    )
    op.create_index(
        "ix_user_task_records_provider_reconcile_due",
        "user_task_records",
        ["next_reconcile_at", "id"],
        unique=False,
        postgresql_where=sa.text(
            "provider_task_id IS NOT NULL AND status IN ('pending', 'running')"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_user_task_records_provider_reconcile_due",
        table_name="user_task_records",
    )
    op.drop_index("ix_user_task_records_provider_task", table_name="user_task_records")
    op.drop_column("user_task_records", "reconcile_attempts")
    op.drop_column("user_task_records", "next_reconcile_at")
    op.drop_column("user_task_records", "last_reconcile_at")
    op.drop_column("user_task_records", "provider_submitted_at")
    op.drop_column("user_task_records", "provider_status")
    op.drop_column("user_task_records", "provider_task_id")
    op.drop_column("user_task_records", "provider_vendor")
