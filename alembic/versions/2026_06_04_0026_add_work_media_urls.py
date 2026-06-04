"""add work media urls

Revision ID: 0026_add_work_media_urls
Revises: 0025_create_user_works
Create Date: 2026-06-04 16:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.core.config import settings

revision: str = "0026_add_work_media_urls"
down_revision: Union[str, None] = "0025_create_user_works"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("user_work_uploads", sa.Column("url", sa.String(length=1024), nullable=True))
    op.add_column("user_work_media", sa.Column("url", sa.String(length=1024), nullable=True))
    base_url = _oss_public_base_url()
    op.execute(
        sa.text("UPDATE user_work_uploads SET url = :base_url || '/' || ltrim(object_key, '/') WHERE url IS NULL")
        .bindparams(base_url=base_url)
    )
    op.execute(
        sa.text("UPDATE user_work_media SET url = :base_url || '/' || ltrim(object_key, '/') WHERE url IS NULL")
        .bindparams(base_url=base_url)
    )
    op.alter_column("user_work_uploads", "url", nullable=False)
    op.alter_column("user_work_media", "url", nullable=False)


def downgrade() -> None:
    op.drop_column("user_work_media", "url")
    op.drop_column("user_work_uploads", "url")


def _oss_public_base_url() -> str:
    if settings.oss_public_base_url:
        return settings.oss_public_base_url.rstrip("/")
    endpoint = settings.oss_endpoint.removeprefix("https://").removeprefix("http://").rstrip("/")
    return f"https://{settings.oss_bucket_name}.{endpoint}"
