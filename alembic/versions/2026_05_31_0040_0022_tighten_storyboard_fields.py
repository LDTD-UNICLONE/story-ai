"""tighten storyboard fields

Revision ID: 0022_tighten_storyboard_fields
Revises: 0021_create_materials
Create Date: 2026-05-31 12:40:00.000000
"""

from typing import Union

import sqlalchemy as sa
from alembic import op


revision: str = "0022_tighten_storyboard_fields"
down_revision: Union[str, None] = "0021_create_materials"
branch_labels: Union[str, tuple[str, ...], None] = None
depends_on: Union[str, tuple[str, ...], None] = None


def upgrade() -> None:
    op.drop_column("project_storyboards", "scene_time")
    op.drop_column("project_storyboards", "emotion")
    op.drop_column("project_storyboards", "visual_description")


def downgrade() -> None:
    op.add_column("project_storyboards", sa.Column("visual_description", sa.Text(), nullable=True))
    op.add_column("project_storyboards", sa.Column("emotion", sa.Text(), nullable=True))
    op.add_column("project_storyboards", sa.Column("scene_time", sa.String(length=64), nullable=True))
