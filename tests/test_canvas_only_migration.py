import os
from datetime import datetime
from uuid import uuid4

import pytest
from sqlalchemy import MetaData, inspect, text

from tests.test_migrations_integration import migration_db  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_DB_INTEGRATION_TESTS") != "1", reason="isolated PostgreSQL"),
]


def seed(fixture):
    fixture.migrate("0066_canvas_generations")
    conn = fixture.connection
    metadata = MetaData()
    metadata.reflect(bind=conn)

    def insert(table, **values):
        values.setdefault("id", uuid4())
        conn.execute(metadata.tables[table].insert().values(**values))
        return values["id"]

    uid = insert("users", account=uuid4().hex, password_hash="test", nickname="Migration")
    pid = insert("projects", user_id=uid, name="Standard", cover="", description="")
    agent = insert("projects", user_id=uid, name="Agent", cover="", description="", project_kind="agent")
    chapter = insert("project_chapters", project_id=pid, user_id=uid, title="Long chapter",
                     content="甲" * 20001, processed_content="Processed", processing_prompt="Rewrite")
    agent_chapter = insert("project_chapters", project_id=agent, user_id=uid, title="Agent", content="Keep")
    asset = insert("project_characters", project_id=pid, user_id=uid, source_chapter_id=chapter,
                   name="Hero", prompt="Draw", aliases=["Alias"], reference_image="https://example.com/a.png")
    board = insert("project_storyboards", project_id=pid, user_id=uid, chapter_id=chapter,
                   title="Shot", source_content="Action", image_prompt="Scene", video_prompt="Animate",
                   extra={"video_generation_result": "https://example.com/video.mp4"})
    task = insert("user_task_records", user_id=uid, business_id=pid, business_type="project",
                  generation_type="asset_image_generate", status="success", title="History", prompt="Draw",
                  points_cost=8, result="https://example.com/a.png")
    history = insert("project_generated_assets", project_id=pid, user_id=uid, target_type="character",
                     target_id=asset, chapter_id=chapter, task_record_id=task, media_type="image",
                     result_url="https://example.com/a.png", result_urls=["https://example.com/a.png", "https://example.com/b.png"])
    video = insert("project_generated_assets", project_id=pid, user_id=uid, target_type="storyboard",
                   target_id=board, chapter_id=chapter, media_type="video", result_url="https://example.com/video.mp4",
                   result_urls=["https://example.com/video.mp4"], last_frame_url="https://example.com/tail.png")
    canvas = insert("project_canvases", project_id=pid, name="Existing", viewport={"x": 3, "y": 4, "zoom": 1})
    nid = insert("canvas_nodes", canvas_id=canvas, kind="text", title="Existing", x=0, y=0,
                 width=320, height=240, content={"text": "Leave intact", "generation": None})
    mid = insert("project_media", project_id=pid, source_key="existing:legacy", source_type="generated_image",
                 source_id=history, upload={"url": "https://example.com/a.png", "filename": "a.png"})
    return locals()


def test_conversion_preserves_content_media_tasks_and_agent(migration_db):  # noqa: F811
    fixture = migration_db
    s = seed(fixture)
    conn, pid = s["conn"], s["pid"]
    before = conn.scalar(text("SELECT row_to_json(t) FROM project_characters t WHERE id=:id"), {"id": s["asset"]})
    fixture.migrate()
    records = list(conn.execute(text("SELECT * FROM canvas_import_records WHERE project_id=:id"), {"id": pid}).mappings())
    assert len(records) == 5
    by_type = {r["source_type"]: r for r in records}
    converted = dict(by_type["character"]["data"])
    for field in ("created_at", "updated_at"):
        assert datetime.fromisoformat(converted.pop(field)) == datetime.fromisoformat(before.pop(field))
    assert converted == before
    original_nodes = [conn.scalar(text("SELECT content->>'text' FROM canvas_nodes WHERE canvas_id=:cid AND id=:id"),
                                {"cid": n["canvas_id"], "id": n["node_id"]}) for n in by_type["chapter"]["nodes"]]
    assert "".join(original_nodes[:3]) == "甲" * 20001
    assert original_nodes[3:] == ["Processed", "Rewrite"]
    assert conn.scalar(text("SELECT content->>'text' FROM canvas_nodes WHERE id=:id"), {"id": s["nid"]}) == "Leave intact"
    for table in ("project_chapters", "project_characters", "project_scenes", "project_props", "project_storyboards", "project_generated_assets"):
        assert conn.scalar(text(f"SELECT count(*) FROM {table} WHERE project_id=:id"), {"id": pid}) == 0
    assert conn.scalar(text("SELECT content FROM project_chapters WHERE id=:id"), {"id": s["agent_chapter"]}) == "Keep"
    assert conn.scalar(text("SELECT count(*) FROM project_media WHERE project_id=:id"), {"id": pid}) == 4
    assert conn.scalar(text("SELECT count(*) FROM user_task_records")) == 1
    assert conn.scalar(text("SELECT points_cost FROM user_task_records WHERE id=:id"), {"id": s["task"]}) == 8
    assert conn.scalar(text("SELECT count(*) FROM canvas_generations")) == 0
    assert conn.scalar(text("SELECT count(*) FROM user_points_transactions")) == 0
    assert conn.scalar(text("SELECT source_type FROM project_media WHERE id=:id"), {"id": s["mid"]}) == "canvas_import"
    total = conn.scalar(text("SELECT count(*) FROM canvas_nodes"))
    fixture.migrate()
    assert conn.scalar(text("SELECT count(*) FROM canvas_nodes")) == total
    with pytest.raises(RuntimeError, match="禁止直接回退"):
        fixture.migrate("0066_canvas_generations", downgrade=True)
    conn.rollback()
    assert conn.scalar(text("SELECT count(*) FROM canvas_nodes")) == total


@pytest.mark.parametrize("case", ["active", "owner", "foreign_chapter", "bad_history"])
def test_failed_conversion_rolls_back_everything(migration_db, case):  # noqa: F811
    fixture = migration_db
    s = seed(fixture)
    conn = s["conn"]
    if case == "active":
        conn.execute(text("UPDATE user_task_records SET status='running' WHERE id=:id"), {"id": s["task"]})
    elif case == "owner":
        uid = s["insert"]("users", account=uuid4().hex, password_hash="test", nickname="Other")
        conn.execute(text("UPDATE project_characters SET user_id=:uid WHERE id=:id"), {"uid": uid, "id": s["asset"]})
    elif case == "foreign_chapter":
        s["insert"]("project_props", project_id=s["agent"], user_id=s["uid"], name="Agent prop", source_chapter_id=s["chapter"])
    else:
        conn.execute(text("UPDATE project_generated_assets SET media_type='audio' WHERE id=:id"), {"id": s["video"]})
    with pytest.raises(RuntimeError):
        fixture.migrate()
    conn.rollback()
    assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0066_canvas_generations"
    assert "canvas_import_records" not in inspect(conn).get_table_names()
    assert conn.scalar(text("SELECT count(*) FROM project_characters WHERE project_id=:id"), {"id": s["pid"]}) == 1
    assert conn.scalar(text("SELECT count(*) FROM canvas_nodes")) == 1


def test_deleted_and_unverified_content_not_resurrected_or_trusted(migration_db):  # noqa: F811
    s = seed(migration_db)
    s["insert"]("project_props", project_id=s["pid"], user_id=s["uid"], name="Deleted", prompt="Keep archived", is_enabled=False)
    s["insert"]("project_scenes", project_id=s["pid"], user_id=s["uid"], name="Unknown image", reference_image="https://example.com/unverified.png")
    migration_db.migrate()
    conn = s["conn"]
    rows = {r.source_type: r for r in conn.execute(text("SELECT * FROM canvas_import_records WHERE source_type IN ('prop','scene')"))}
    assert rows["prop"].nodes == [] and rows["prop"].data["prompt"] == "Keep archived"
    assert rows["prop"].warnings[0]["reason"] == "deleted_source"
    assert rows["scene"].warnings[0]["reason"] == "unverified_media"
    mid = conn.scalar(text("SELECT media_id FROM canvas_nodes WHERE import_record_id=:id"), {"id": rows["scene"].id})
    media = conn.execute(text("SELECT source_verified, upload FROM project_media WHERE id=:id"), {"id": mid}).one()
    assert media.source_verified is False
    assert media.upload["url"] == "https://example.com/unverified.png"


def test_large_text_splits_canvases_without_loss(migration_db):  # noqa: F811
    s = seed(migration_db)
    full = "段" * 4_010_001
    s["conn"].execute(text("UPDATE project_chapters SET content=:content WHERE id=:id"), {"content": full, "id": s["chapter"]})
    migration_db.migrate()
    conn = s["conn"]
    row = conn.execute(text("SELECT * FROM canvas_import_records WHERE source_type='chapter'")).mappings().one()
    chunks = []
    for location in row["nodes"][:402]:
        chunks.append(conn.scalar(text("SELECT content->>'text' FROM canvas_nodes WHERE canvas_id=:cid AND id=:id"),
                                  {"cid": location["canvas_id"], "id": location["node_id"]}))
    assert "".join(chunks) == full
    assert len({x["canvas_id"] for x in row["nodes"]}) == 2
    assert conn.scalar(text("SELECT max(n) FROM (SELECT count(*) n FROM canvas_nodes GROUP BY canvas_id) s")) <= 400


def test_database_disallows_standard_legacy_writes_but_accepts_agent(migration_db):  # noqa: F811
    s = seed(migration_db)
    migration_db.migrate()
    conn = s["conn"]
    for statement in [
        "INSERT INTO project_chapters(id,project_id,user_id,title,content) VALUES (:id,:pid,:uid,'New','Old mode')",
        "INSERT INTO user_task_records(id,user_id,business_id,business_type,generation_type,status,title,prompt) VALUES (:id,:uid,:pid,'project','chapter_text_process','pending','New','Old mode')",
    ]:
        with pytest.raises(Exception, match="Standard project"):
            with conn.begin_nested():
                conn.execute(text(statement), {"id": uuid4(), "pid": s["pid"], "uid": s["uid"]})
    conn.execute(text("INSERT INTO project_chapters(id,project_id,user_id,title,content) VALUES (:id,:pid,:uid,'Agent','Allowed')"),
                 {"id": uuid4(), "pid": s["agent"], "uid": s["uid"]})
    assert conn.scalar(text("SELECT count(*) FROM project_chapters WHERE project_id=:pid"), {"pid": s["agent"]}) == 2
