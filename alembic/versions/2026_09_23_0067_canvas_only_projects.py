"""Convert standard project content into canvas nodes; retain Agent storage only."""

import hashlib
import json
from uuid import NAMESPACE_URL, uuid5

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0067_canvas_only_projects"
down_revision = "0066_canvas_generations"
branch_labels = None
depends_on = None

SOURCES = {
    "project_chapters": "chapter",
    "project_characters": "character",
    "project_scenes": "scene",
    "project_props": "prop",
    "project_storyboards": "storyboard",
    "project_generated_assets": "generated_asset",
}


def identity(*parts):
    return uuid5(NAMESPACE_URL, "story-ai:canvas-conversion:v1:" + ":".join(map(str, parts)))


def json_value(value):
    return json.loads(json.dumps(value, default=lambda x: x.isoformat() if hasattr(x, "isoformat") else str(x)))


def preflight(conn, metadata):
    # Stop writers for the conversion transaction. Deploy with old API/workers drained.
    tables = ["projects", "user_task_records", *SOURCES]
    conn.execute(sa.text("LOCK TABLE " + ", ".join(tables) + " IN SHARE ROW EXCLUSIVE MODE"))
    if conn.scalar(sa.text("""
        SELECT EXISTS (SELECT 1 FROM agent_productions a JOIN projects p ON p.id=a.project_id
        WHERE p.project_kind='standard')
    """)):
        raise RuntimeError("仍有 Agent 制作绑定普通项目，请先核对并分离 Agent 项目，禁止误删其内部内容")
    if conn.scalar(sa.text("""
        SELECT EXISTS (SELECT 1 FROM user_task_records t JOIN projects p ON p.id=t.business_id
        WHERE p.project_kind='standard' AND t.business_type='project'
          AND t.status IN ('pending','running') AND t.extra->>'canvas_generation_id' IS NULL)
    """)):
        raise RuntimeError("普通项目仍有章节模式任务未结束；请停止旧入口并等待任务完成，不能迁移或强制退款")
    for table in SOURCES:
        if conn.scalar(sa.text(f"""
            SELECT EXISTS (SELECT 1 FROM {table} s JOIN projects p ON p.id=s.project_id
            WHERE p.project_kind='standard' AND s.user_id<>p.user_id)
        """)):
            raise RuntimeError(f"{table} 存在所属用户不一致的数据；已停止迁移")
    # Never cascade-delete an Agent record through a wrongly linked standard chapter.
    for table in metadata.tables.values():
        for fk in table.foreign_key_constraints:
            if fk.referred_table.name not in SOURCES:
                continue
            joins = [table.c[e.parent.name] == e.column for e in fk.elements]
            source = fk.referred_table
            project = metadata.tables["projects"]
            condition = project.c.project_kind == "standard"
            if table.name in SOURCES:
                condition = sa.and_(condition, table.c.project_id != source.c.project_id)
            found = conn.scalar(sa.select(sa.func.count()).select_from(
                table.join(source, sa.and_(*joins)).join(project, source.c.project_id == project.c.id)
            ).where(condition))
            if found:
                raise RuntimeError(f"{table.name} 仍引用普通项目旧数据；请先核对归属，禁止级联删除")


class Converter:
    def __init__(self, conn, metadata, project):
        self.conn, self.tables, self.project = conn, metadata.tables, project
        self.pages = []
        self.media = {}
        self.trusted = set()
        self.records = {}
        self.rows = {}
        for name in SOURCES:
            table = self.tables[name]
            self.rows[name] = list(conn.execute(sa.select(table).where(
                table.c.project_id == project["id"]
            ).order_by(table.c.created_at, table.c.id)).mappings())
        for row in conn.execute(sa.select(self.tables["project_media"]).where(
            self.tables["project_media"].c.project_id == project["id"]
        )).mappings():
            url = row["upload"].get("url")
            self.media.setdefault((row["media_type"], url), row["id"])
            if row["source_verified"]:
                self.trusted.add((row["media_type"], url))
        for row in self.rows["project_generated_assets"]:
            if row["status"] == "success":
                for url in [row["result_url"], *(row["result_urls"] or [])]:
                    if isinstance(url, str):
                        self.trusted.add((row["media_type"], url))
                if row["last_frame_url"]:
                    self.trusted.add(("image", row["last_frame_url"]))
        images = self.tables["seedance_images"]
        for upload in conn.scalars(sa.select(images.c.upload).where(images.c.user_id == project["user_id"])):
            for url in [upload.get("url"), *(upload.get("source_urls") or [])]:
                if isinstance(url, str):
                    self.trusted.add(("image", url))

    def record(self, table, row):
        rid = identity(self.project["id"], table, row["id"])
        value = dict(id=rid, project_id=self.project["id"], source_type=SOURCES[table],
                     source_id=row["id"], data=json_value(dict(row)), nodes=[], warnings=[])
        self.conn.execute(self.tables["canvas_import_records"].insert().values(**value))
        self.records[(table, row["id"])] = value
        return value

    def node(self, record, kind, title, text="", media_id=None):
        if not self.pages or self.pages[-1][1] >= 400:
            cid = identity(self.project["id"], "canvas", len(self.pages))
            self.conn.execute(self.tables["project_canvases"].insert().values(
                id=cid, project_id=self.project["id"], name=f"历史内容 {len(self.pages) + 1}",
                revision=1, viewport={"x": 0, "y": 0, "zoom": 1},
            ))
            self.pages.append([cid, 0])
        cid, index = self.pages[-1]
        nid = identity(record["id"], "node", len(record["nodes"]))
        self.conn.execute(self.tables["canvas_nodes"].insert().values(
            canvas_id=cid, id=nid, kind=kind, title=title[:128],
            x=(index % 4) * 400, y=(index // 4) * 320, width=320, height=240,
            parent_id=None, media_id=media_id, import_record_id=record["id"],
            content={"text": text[:10000], "generation": None}, content_revision=1,
        ))
        record["nodes"].append({"canvas_id": str(cid), "node_id": str(nid)})
        self.pages[-1][1] += 1
        # Preserve editable long prompts in additional text nodes, never silently truncate.
        for offset in range(10000, len(text), 10000):
            self.node(record, "text", f"{title} · 续文 {offset // 10000}", text[offset:offset + 10000])

    def media_id(self, record, kind, url):
        if not url:
            return None
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            record["warnings"].append({"reason": "unverified_media", "url": url})
            return None
        verified = (kind, url) in self.trusted
        if not verified:
            record["warnings"].append({"reason": "unverified_media", "url": url})
        if (kind, url) not in self.media:
            mid = identity(self.project["id"], "media", kind, url)
            self.conn.execute(self.tables["project_media"].insert().values(
                id=mid, project_id=self.project["id"], media_type=kind,
                source_key=f"converted:{kind}:{hashlib.sha256(url.encode()).hexdigest()}",
                source_type="canvas_import", source_id=record["id"],
                source_verified=verified,
                upload={"url": url, "filename": kind},
            ))
            self.media[(kind, url)] = mid
        return self.media[(kind, url)]

    def run(self):
        chapters = {row["id"]: row for row in self.rows["project_chapters"]}
        for table, rows in self.rows.items():
            for row in rows:
                record = self.record(table, row)
                title = row.get("title") or row.get("name") or "历史生成结果"
                active = row["is_enabled"]
                if table in {"project_storyboards", "project_generated_assets"} and row.get("chapter_id"):
                    chapter = chapters.get(row["chapter_id"])
                    if chapter is None:
                        raise RuntimeError("普通项目分镜/历史引用了其他项目章节；已停止迁移")
                    active = active and chapter["is_enabled"]
                if not active:
                    record["warnings"].append({"reason": "deleted_source", "message": "保留原数据，不恢复已删除内容到编辑画布"})
                elif table == "project_chapters":
                    for key, label in [("content", "原文"), ("processed_content", "处理结果"), ("processing_prompt", "处理提示词")]:
                        if row[key]:
                            self.node(record, "text", f"{title} · {label}", row[key])
                elif table == "project_storyboards":
                    self.node(record, "text", f"{title} · 文本", row["source_content"])
                    for kind in ("image", "video"):
                        url = (row["extra"] or {}).get(f"{kind}_generation_result")
                        mid = self.media_id(record, kind, url)
                        self.node(record, kind, title, row[f"{kind}_prompt"] or "", mid)
                elif table == "project_generated_assets":
                    kind = row["media_type"]
                    if kind not in {"image", "video"}:
                        raise RuntimeError("历史中存在无法识别的媒体类型；已停止迁移")
                    urls = list(dict.fromkeys([url for url in [row["result_url"], *(row["result_urls"] or [])] if url]))
                    for index, url in enumerate(urls):
                        self.node(record, kind, f"历史候选 {index + 1}", row["prompt"] or "",
                                  self.media_id(record, kind, url))
                    if row["last_frame_url"]:
                        self.node(record, "image", "历史视频尾帧", media_id=self.media_id(record, "image", row["last_frame_url"]))
                else:
                    self.node(record, "image", title, row["prompt"] or "",
                              self.media_id(record, "image", row["reference_image"]))
                self.conn.execute(self.tables["canvas_import_records"].update().where(
                    self.tables["canvas_import_records"].c.id == record["id"]
                ).values(nodes=record["nodes"], warnings=record["warnings"]))
        # Repoint pre-existing references to histories that are about to be removed.
        media = self.tables["project_media"]
        for (table, source_id), record in self.records.items():
            if table == "project_generated_assets":
                self.conn.execute(media.update().where(
                    media.c.project_id == self.project["id"], media.c.source_id == source_id,
                    media.c.source_type.in_(["generated_image", "generated_last_frame", "generated_video"]),
                ).values(source_type="canvas_import", source_id=record["id"]))
        for table in ["project_generated_assets", "project_storyboards", "project_characters", "project_scenes", "project_props", "project_chapters"]:
            self.conn.execute(self.tables[table].delete().where(self.tables[table].c.project_id == self.project["id"]))


def upgrade():
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        raise RuntimeError("画布数据转换要求在线 PostgreSQL 事务，不支持离线 SQL")
    metadata = sa.MetaData()
    metadata.reflect(bind=conn)
    preflight(conn, metadata)
    op.add_column("project_media", sa.Column("source_verified", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.create_table(
        "canvas_import_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False), sa.Column("nodes", sa.JSON(), nullable=False),
        sa.Column("warnings", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("project_id", "source_type", "source_id", name="uq_canvas_import_records_project_id"),
    )
    op.create_index("ix_canvas_import_records_project_id", "canvas_import_records", ["project_id"])
    op.add_column("canvas_nodes", sa.Column("import_record_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key("fk_canvas_nodes_import_record_id_canvas_import_records", "canvas_nodes", "canvas_import_records", ["import_record_id"], ["id"], ondelete="SET NULL")
    metadata = sa.MetaData()
    metadata.reflect(bind=conn)
    projects = list(conn.execute(sa.select(metadata.tables["projects"]).where(
        metadata.tables["projects"].c.project_kind == "standard"
    )).mappings())
    for project in projects:
        Converter(conn, metadata, project).run()
    op.execute("""
        CREATE FUNCTION require_agent_content() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF EXISTS (SELECT 1 FROM projects WHERE id=NEW.project_id AND project_kind='standard') THEN
            RAISE EXCEPTION 'Standard projects only support canvas content';
          END IF;
          RETURN NEW;
        END $$
    """)
    for table in SOURCES:
        op.execute(f"CREATE TRIGGER canvas_only_content BEFORE INSERT OR UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION require_agent_content()")
    op.execute("""
        CREATE FUNCTION require_canvas_project_task() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.business_type='project' AND NEW.extra->>'canvas_generation_id' IS NULL
            AND EXISTS (SELECT 1 FROM projects WHERE id=NEW.business_id AND project_kind='standard') THEN
            RAISE EXCEPTION 'Standard project tasks must originate from canvas generation';
          END IF;
          RETURN NEW;
        END $$
    """)
    op.execute("CREATE TRIGGER canvas_only_task BEFORE INSERT ON user_task_records FOR EACH ROW EXECUTE FUNCTION require_canvas_project_task()")


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM canvas_import_records)")):
        raise RuntimeError("已转换旧项目数据，禁止直接回退丢失画布编辑；请使用迁移前完整备份恢复")
    op.execute("DROP TRIGGER canvas_only_task ON user_task_records")
    op.execute("DROP FUNCTION require_canvas_project_task()")
    for table in SOURCES:
        op.execute(f"DROP TRIGGER canvas_only_content ON {table}")
    op.execute("DROP FUNCTION require_agent_content()")
    op.drop_constraint("fk_canvas_nodes_import_record_id_canvas_import_records", "canvas_nodes", type_="foreignkey")
    op.drop_column("canvas_nodes", "import_record_id")
    op.drop_index("ix_canvas_import_records_project_id", table_name="canvas_import_records")
    op.drop_table("canvas_import_records")
    op.drop_column("project_media", "source_verified")
