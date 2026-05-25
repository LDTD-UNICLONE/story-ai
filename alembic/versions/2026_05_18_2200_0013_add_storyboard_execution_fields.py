"""add storyboard execution fields

Revision ID: 0013_add_storyboard_execution_fields
Revises: 0012_create_project_storyboards
Create Date: 2026-05-18 22:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0013_add_storyboard_execution_fields"
down_revision: Union[str, None] = "0012_create_project_storyboards"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "alembic_version",
        "version_num",
        existing_type=sa.String(length=32),
        type_=sa.String(length=128),
        existing_nullable=False,
    )
    op.add_column("project_storyboards", sa.Column("scene_time", sa.String(length=64), nullable=True))
    op.add_column("project_storyboards", sa.Column("shot_size", sa.String(length=64), nullable=True))
    op.add_column("project_storyboards", sa.Column("camera_angle", sa.String(length=128), nullable=True))
    op.add_column("project_storyboards", sa.Column("camera_movement", sa.Text(), nullable=True))
    op.add_column("project_storyboards", sa.Column("screen_execution", sa.Text(), nullable=True))
    op.add_column("project_storyboards", sa.Column("character_action", sa.Text(), nullable=True))
    op.add_column("project_storyboards", sa.Column("character_expression", sa.Text(), nullable=True))
    op.add_column("project_storyboards", sa.Column("sound_effect", sa.Text(), nullable=True))
    op.add_column("project_storyboards", sa.Column("production_focus", sa.Text(), nullable=True))
    op.add_column("project_storyboards", sa.Column("negative_prompt", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("project_storyboards", "negative_prompt")
    op.drop_column("project_storyboards", "production_focus")
    op.drop_column("project_storyboards", "sound_effect")
    op.drop_column("project_storyboards", "character_expression")
    op.drop_column("project_storyboards", "character_action")
    op.drop_column("project_storyboards", "screen_execution")
    op.drop_column("project_storyboards", "camera_movement")
    op.drop_column("project_storyboards", "camera_angle")
    op.drop_column("project_storyboards", "shot_size")
    op.drop_column("project_storyboards", "scene_time")
