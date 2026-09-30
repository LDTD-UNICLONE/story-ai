"""Persist upload-time Seedance image reviews and content deduplication."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0063_seedance_images"
down_revision = "0062_task_dispatch_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "seedance_images",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_scope", sa.String(64), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("upload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("provider_task_id", sa.String(255), nullable=True),
        sa.Column("asset_url", sa.String(512), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("poll_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_seedance_images")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
            name=op.f("fk_seedance_images_user_id_users"),
        ),
        sa.UniqueConstraint(
            "user_id", "provider_scope", "sha256", name="uq_seedance_images_user_scope_hash"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'submitting', 'processing', 'ready', 'failed', 'uncertain')",
            name=op.f("ck_seedance_images_status"),
        ),
    )
    op.create_index("ix_seedance_images_review_due", "seedance_images", ["next_poll_at"])


def downgrade() -> None:
    op.drop_index("ix_seedance_images_review_due", table_name="seedance_images")
    op.drop_table("seedance_images")
