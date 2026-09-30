"""Persist project image versions and pin canvas inputs."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0065_project_media"
down_revision = "0064_project_canvases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_media",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_key", sa.String(160), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("upload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("project_id", "source_key", name="uq_project_media_source"),
    )
    op.create_index("ix_project_media_project_id", "project_media", ["project_id"])
    for table in ("canvas_nodes", "canvas_edges"):
        op.add_column(table, sa.Column("media_id", postgresql.UUID(as_uuid=True), nullable=True))
        op.create_foreign_key(
            f"fk_{table}_media_id_project_media", table, "project_media", ["media_id"], ["id"]
        )


def downgrade() -> None:
    for table in ("canvas_edges", "canvas_nodes"):
        op.drop_constraint(f"fk_{table}_media_id_project_media", table, type_="foreignkey")
        op.drop_column(table, "media_id")
    op.drop_index("ix_project_media_project_id", table_name="project_media")
    op.drop_table("project_media")
