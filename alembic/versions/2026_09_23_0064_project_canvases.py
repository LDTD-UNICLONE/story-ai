"""Add independent project canvas editing data."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0064_project_canvases"
down_revision = "0063_seedance_images"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_canvases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("revision", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("viewport", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("revision >= 1", name="revision_positive"),
    )
    op.create_index("ix_project_canvases_project_id", "project_canvases", ["project_id"])
    op.create_table(
        "canvas_nodes",
        sa.Column(
            "canvas_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project_canvases.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("title", sa.String(128), nullable=False),
        sa.Column("x", sa.Float(), nullable=False),
        sa.Column("y", sa.Float(), nullable=False),
        sa.Column("width", sa.Float(), nullable=False),
        sa.Column("height", sa.Float(), nullable=False),
        sa.Column("parent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("content_revision", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.CheckConstraint("kind IN ('text','image','video','group')", name="kind"),
        sa.CheckConstraint("width > 0 AND height > 0", name="positive_size"),
        sa.CheckConstraint("content_revision >= 1", name="content_revision_positive"),
        sa.ForeignKeyConstraint(
            ["canvas_id", "parent_id"],
            ["canvas_nodes.canvas_id", "canvas_nodes.id"],
            name="fk_canvas_nodes_parent",
            deferrable=True,
            initially="DEFERRED",
        ),
    )
    op.create_table(
        "canvas_edges",
        sa.Column(
            "canvas_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project_canvases.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("input", sa.String(16), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["canvas_id", "source_id"],
            ["canvas_nodes.canvas_id", "canvas_nodes.id"],
            name="fk_canvas_edges_source",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["canvas_id", "target_id"],
            ["canvas_nodes.canvas_id", "canvas_nodes.id"],
            name="fk_canvas_edges_target",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "canvas_id",
            "target_id",
            "input",
            "position",
            name="uq_canvas_edges_input_position",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.CheckConstraint("source_id <> target_id", name="not_self"),
        sa.CheckConstraint("input IN ('reference','first_frame','last_frame')", name="input"),
        sa.CheckConstraint("position >= 0", name="position_nonnegative"),
    )


def downgrade() -> None:
    op.drop_table("canvas_edges")
    op.drop_table("canvas_nodes")
    op.drop_index("ix_project_canvases_project_id", table_name="project_canvases")
    op.drop_table("project_canvases")
