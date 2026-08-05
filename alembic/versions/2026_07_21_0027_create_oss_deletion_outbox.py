"""create oss deletion outbox

Revision ID: 0027_create_oss_deletion_outbox
Revises: 0026_add_work_media_urls
Create Date: 2026-07-21 14:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0027_create_oss_deletion_outbox"
down_revision: Union[str, None] = "0026_add_work_media_urls"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "oss_deletion_outbox",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_oss_deletion_outbox")),
        sa.UniqueConstraint("object_key", name=op.f("uq_oss_deletion_outbox_object_key")),
    )
    op.create_index(
        op.f("ix_oss_deletion_outbox_next_attempt_at"),
        "oss_deletion_outbox",
        ["next_attempt_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_oss_deletion_outbox_next_attempt_at"),
        table_name="oss_deletion_outbox",
    )
    op.drop_table("oss_deletion_outbox")
