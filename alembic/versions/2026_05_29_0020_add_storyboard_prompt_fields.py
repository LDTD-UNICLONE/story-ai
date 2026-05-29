"""add storyboard video prompt fields

Revision ID: 0020_add_storyboard_prompt_fields
Revises: 0019_add_announcement_content_format
Create Date: 2026-05-29 16:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0020_add_storyboard_prompt_fields"
down_revision: Union[str, None] = "0019_add_announcement_content_format"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("project_storyboards", sa.Column("scene_state", sa.String(length=128), nullable=True))
    op.add_column("project_storyboards", sa.Column("atmosphere", sa.Text(), nullable=True))
    op.add_column("project_storyboards", sa.Column("ending_frame", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("project_storyboards", "ending_frame")
    op.drop_column("project_storyboards", "atmosphere")
    op.drop_column("project_storyboards", "scene_state")
