"""ensure one selected media version per generated target

Revision ID: 0040_storyboard_primary_video
Revises: 0039_storyboard_media_requests
Create Date: 2026-08-03 16:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0040_storyboard_primary_video"
down_revision: Union[str, None] = "0039_storyboard_media_requests"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        WITH ranked AS (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY project_id, user_id, target_type, target_id, media_type
                       ORDER BY updated_at DESC, created_at DESC, id DESC
                   ) AS row_number
            FROM project_generated_assets
            WHERE is_selected IS TRUE
        )
        UPDATE project_generated_assets AS history
        SET is_selected = FALSE
        FROM ranked
        WHERE history.id = ranked.id AND ranked.row_number > 1
        """
    )
    op.create_index(
        "uq_project_generated_assets_selected_target",
        "project_generated_assets",
        ["project_id", "user_id", "target_type", "target_id", "media_type"],
        unique=True,
        postgresql_where=sa.text("is_selected"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_project_generated_assets_selected_target",
        table_name="project_generated_assets",
    )
