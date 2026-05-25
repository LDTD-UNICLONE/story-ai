"""transfer project owner

Revision ID: 0016_transfer_project_owner
Revises: 0015_add_performance_indexes
Create Date: 2026-05-21 22:30:00.000000
"""

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "0016_transfer_project_owner"
down_revision: Union[str, None] = "0015_add_performance_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PROJECT_ID = "0c52ab99-6f1c-472c-b84b-e8216f1d45c2"
TARGET_USER_ID = "a832ecce-10e3-41b1-bb0b-78d88105b709"

PROJECT_OWNER_TABLES = (
    "projects",
    "project_chapters",
    "project_characters",
    "project_scenes",
    "project_props",
    "project_storyboards",
)


def upgrade() -> None:
    bind = op.get_bind()
    params = {
        "project_id": PROJECT_ID,
        "project_id_text": PROJECT_ID,
        "target_user_id": TARGET_USER_ID,
    }

    target_user_exists = bind.execute(
        text("SELECT 1 FROM users WHERE id = CAST(:target_user_id AS uuid)"),
        params,
    ).scalar_one_or_none()
    if target_user_exists is None:
        print(f"Skip project owner transfer, target user does not exist: {TARGET_USER_ID}")
        return

    project_exists = bind.execute(
        text("SELECT 1 FROM projects WHERE id = CAST(:project_id AS uuid)"),
        params,
    ).scalar_one_or_none()
    if project_exists is None:
        print(f"Skip project owner transfer, project does not exist: {PROJECT_ID}")
        return

    for table_name in PROJECT_OWNER_TABLES:
        bind.execute(
            text(
                f"""
                UPDATE {table_name}
                SET user_id = CAST(:target_user_id AS uuid),
                    updated_at = now()
                WHERE project_id = CAST(:project_id AS uuid)
                  AND user_id <> CAST(:target_user_id AS uuid)
                """
                if table_name != "projects"
                else f"""
                UPDATE {table_name}
                SET user_id = CAST(:target_user_id AS uuid),
                    updated_at = now()
                WHERE id = CAST(:project_id AS uuid)
                  AND user_id <> CAST(:target_user_id AS uuid)
                """
            ),
            params,
        )

    # Transfer project-related task records so the new owner can see project task history.
    # Point transaction rows are intentionally not transferred, preserving original spend history.
    bind.execute(
        text(
            """
            UPDATE user_task_records
            SET user_id = CAST(:target_user_id AS uuid),
                updated_at = now()
            WHERE business_type = 'project'
              AND (
                    business_id = CAST(:project_id AS uuid)
                    OR extra ->> 'project_id' = :project_id_text
                  )
              AND user_id <> CAST(:target_user_id AS uuid)
            """
        ),
        params,
    )


def downgrade() -> None:
    # This one-off migration intentionally has no automatic rollback because the
    # original owner is runtime data and may not be safe to infer later.
    pass
