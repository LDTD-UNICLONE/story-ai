"""Frozen canvas generation requests, history and selection fences."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0066_canvas_generations"
down_revision = "0065_project_media"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "project_media",
        sa.Column("media_type", sa.String(16), nullable=False, server_default="image"),
    )
    for name in ("latest_generation_id", "selected_generation_id"):
        op.add_column("canvas_nodes", sa.Column(name, postgresql.UUID(as_uuid=True), nullable=True))
    op.create_table(
        "canvas_generations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("canvas_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("node_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("content_revision", sa.Integer(), nullable=False),
        sa.Column(
            "task_record_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user_task_records.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "project_id",
            "canvas_id",
            "node_id",
            "idempotency_key",
            name="uq_canvas_generation_request",
        ),
    )
    op.create_index("ix_canvas_generations_project_id", "canvas_generations", ["project_id"])


def downgrade():
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM project_media WHERE media_type = 'video')")
    ):
        raise RuntimeError("存在画布视频媒体，旧版本不能识别；请先备份并处理视频绑定后再回退")
    op.drop_index("ix_canvas_generations_project_id", table_name="canvas_generations")
    op.drop_table("canvas_generations")
    for name in ("selected_generation_id", "latest_generation_id"):
        op.drop_column("canvas_nodes", name)
    op.drop_column("project_media", "media_type")
