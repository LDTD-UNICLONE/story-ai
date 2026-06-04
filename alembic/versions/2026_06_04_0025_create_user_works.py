"""create user works

Revision ID: 0025_create_user_works
Revises: 0024_add_admin_task_record_indexes
Create Date: 2026-06-04 10:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0025_create_user_works"
down_revision: Union[str, None] = "0024_add_admin_task_record_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_works",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("visibility", sa.String(length=16), server_default=sa.text("'public'"), nullable=False),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'published'"), nullable=False),
        sa.Column("like_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("view_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_user_works_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_works")),
    )
    op.create_index(op.f("ix_user_works_user_id"), "user_works", ["user_id"], unique=False)
    op.create_index(op.f("ix_user_works_visibility"), "user_works", ["visibility"], unique=False)
    op.create_index(op.f("ix_user_works_status"), "user_works", ["status"], unique=False)
    op.create_index(
        "ix_user_works_public_rank",
        "user_works",
        ["visibility", "status", "is_enabled", "like_count", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_user_works_owner_created",
        "user_works",
        ["user_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "user_work_media",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("work_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("media_type", sa.String(length=16), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("thumbnail_object_key", sa.String(length=512), nullable=True),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        sa.Column("size", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["work_id"], ["user_works.id"], name=op.f("fk_user_work_media_work_id_user_works"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_work_media")),
        sa.UniqueConstraint("object_key", name=op.f("uq_user_work_media_object_key")),
    )
    op.create_index(op.f("ix_user_work_media_work_id"), "user_work_media", ["work_id"], unique=False)
    op.create_index(op.f("ix_user_work_media_media_type"), "user_work_media", ["media_type"], unique=False)
    op.create_index("ix_user_work_media_work_order", "user_work_media", ["work_id", "sort_order"], unique=False)

    op.create_table(
        "user_work_likes",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("work_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_user_work_likes_user_id_users"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["work_id"], ["user_works.id"], name=op.f("fk_user_work_likes_work_id_user_works"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_work_likes")),
        sa.UniqueConstraint("work_id", "user_id", name="uq_user_work_likes_work_user"),
    )
    op.create_index(op.f("ix_user_work_likes_work_id"), "user_work_likes", ["work_id"], unique=False)
    op.create_index(op.f("ix_user_work_likes_user_id"), "user_work_likes", ["user_id"], unique=False)

    op.create_table(
        "user_work_uploads",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("media_type", sa.String(length=16), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        sa.Column("size", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("is_used", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_user_work_uploads_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_work_uploads")),
        sa.UniqueConstraint("object_key", name=op.f("uq_user_work_uploads_object_key")),
    )
    op.create_index(op.f("ix_user_work_uploads_user_id"), "user_work_uploads", ["user_id"], unique=False)
    op.create_index(op.f("ix_user_work_uploads_media_type"), "user_work_uploads", ["media_type"], unique=False)
    op.create_index(op.f("ix_user_work_uploads_is_used"), "user_work_uploads", ["is_used"], unique=False)
    op.create_index("ix_user_work_uploads_user_used", "user_work_uploads", ["user_id", "is_used"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_user_work_uploads_user_used", table_name="user_work_uploads")
    op.drop_index(op.f("ix_user_work_uploads_is_used"), table_name="user_work_uploads")
    op.drop_index(op.f("ix_user_work_uploads_media_type"), table_name="user_work_uploads")
    op.drop_index(op.f("ix_user_work_uploads_user_id"), table_name="user_work_uploads")
    op.drop_table("user_work_uploads")

    op.drop_index(op.f("ix_user_work_likes_user_id"), table_name="user_work_likes")
    op.drop_index(op.f("ix_user_work_likes_work_id"), table_name="user_work_likes")
    op.drop_table("user_work_likes")

    op.drop_index("ix_user_work_media_work_order", table_name="user_work_media")
    op.drop_index(op.f("ix_user_work_media_media_type"), table_name="user_work_media")
    op.drop_index(op.f("ix_user_work_media_work_id"), table_name="user_work_media")
    op.drop_table("user_work_media")

    op.drop_index("ix_user_works_owner_created", table_name="user_works")
    op.drop_index("ix_user_works_public_rank", table_name="user_works")
    op.drop_index(op.f("ix_user_works_status"), table_name="user_works")
    op.drop_index(op.f("ix_user_works_visibility"), table_name="user_works")
    op.drop_index(op.f("ix_user_works_user_id"), table_name="user_works")
    op.drop_table("user_works")
