"""Pinned text references for canvas inputs."""

from alembic import op
import sqlalchemy as sa

revision = "0068_canvas_text_references"
down_revision = "0067_canvas_only_projects"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("canvas_edges", sa.Column("text_source", sa.String(16), nullable=True))
    op.add_column("canvas_edges", sa.Column("text_snapshot", sa.JSON(), nullable=True))
    op.drop_constraint(op.f("ck_canvas_edges_input"), "canvas_edges", type_="check")
    op.create_check_constraint("input", "canvas_edges", "input IN ('reference','first_frame','last_frame','text')")


def downgrade():
    if op.get_bind().execute(sa.text("SELECT EXISTS(SELECT 1 FROM canvas_edges WHERE input='text')")).scalar():
        raise RuntimeError("存在文本引用连接，不能降级并丢弃引用；请先处理这些连接")
    op.drop_constraint(op.f("ck_canvas_edges_input"), "canvas_edges", type_="check")
    op.create_check_constraint("input", "canvas_edges", "input IN ('reference','first_frame','last_frame')")
    op.drop_column("canvas_edges", "text_snapshot")
    op.drop_column("canvas_edges", "text_source")
