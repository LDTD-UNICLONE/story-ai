# ruff: noqa: F811
import asyncio
import os
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from app.api.deps import get_current_user
from app.api.v1.router import api_router
from app.core.exceptions import AppException, register_exception_handlers
from app.db.session import get_db
from app.models.project import Project
from app.models.project_media import ProjectMedia
from app.models.style import Style
from app.models.task_record import UserTaskRecord
from app.models.task_dispatch import TaskDispatchOutbox
from app.schemas.project_canvas import CanvasCreate, CanvasLayoutPatch
from app.services.projects import canvases
from tests.test_canvas_generations_integration import ctx, setup_node, submit, picture  # noqa: F401
from tests.test_project_canvases_integration import canvas_ctx, patch  # noqa: F401
from tests.test_task_lifecycle_integration import lifecycle_db  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.skipif(os.getenv("RUN_DB_INTEGRATION_TESTS") != "1", reason="isolated PostgreSQL"),
]


@pytest.fixture
async def client(ctx):
    app = FastAPI()
    app.include_router(api_router, prefix="/api/v1")
    register_exception_handlers(app)

    async def session():
        yield ctx.db

    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=ctx.uid)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        client.test_app = app
        yield client


def canvas_path(ctx):
    return f"/api/v1/projects/{ctx.pid}/canvases/{ctx.cid}"


async def test_standard_project_contract_removes_settings_without_erasing_stored_data(ctx, client):
    path = "/api/v1/projects"
    created = await client.post(path, json={"name": "Canvas", "description": "Description"})
    assert created.status_code == 200
    data = created.json()["data"]
    assert not {"style", "style_id", "generation_ratio"} & data.keys()
    stored = await ctx.db.get(Project, UUID(data["id"]))
    assert stored.style_id is None and stored.generation_ratio is None
    style = Style(name=str(uuid4()), cover="", prompt="Agent style", is_enabled=False)
    ctx.db.add(style)
    await ctx.db.flush()
    stored.style_id, stored.generation_ratio = style.id, "16:9"
    await ctx.db.commit()
    detail_path = f"{path}/{stored.id}"
    for field, value in [("style_id", str(style.id)), ("generation_ratio", "9:16"), ("style", {})]:
        assert (await client.post(path, json={"name": "Invalid", field: value})).status_code == 422
        assert (await client.patch(detail_path, json={field: value})).status_code == 422
    updated = await client.patch(detail_path, json={"name": "Renamed"})
    assert updated.status_code == 200
    for response in (updated, await client.get(detail_path)):
        assert not {"style", "style_id", "generation_ratio"} & response.json()["data"].keys()
    assert all(not {"style", "style_id", "generation_ratio"} & item.keys()
               for item in (await client.get(path)).json()["data"]["items"])
    await ctx.db.refresh(stored)
    assert stored.style_id == style.id and stored.generation_ratio == "16:9"


async def test_layout_is_partial_and_preserves_generation_state(ctx, client):
    n, _ = await setup_node(ctx)
    media = await picture(ctx)
    n.media_id = media.id
    await patch(ctx, 2, upsert_nodes=[n])
    await submit(ctx, n, revision=2)
    path = canvas_path(ctx)
    before = (await client.get(path)).json()["data"]
    response = await client.patch(path + "/layout", json={
        "expected_revision": before["revision"], "nodes": [{"id": str(n.id), "x": 900, "height": 300}],
        "viewport": {"x": 20, "y": 40, "zoom": 0.8},
    })
    assert response.status_code == 200
    result = response.json()["data"]
    assert result["revision"] == before["revision"] + 1
    assert set(result["nodes"][0]) == {"id", "x", "y", "width", "height"}
    after = (await client.get(path)).json()["data"]
    expected = {**before["nodes"][0], "x": 900, "height": 300}
    assert after["nodes"] == [expected]
    assert after["viewport"] == {"x": 20, "y": 40, "zoom": 0.8}
    assert await ctx.db.scalar(select(func.count()).select_from(UserTaskRecord)) == 1
    stale = await client.patch(path + "/layout", json={
        "expected_revision": before["revision"], "nodes": [{"id": str(n.id), "x": 0}],
    })
    assert stale.status_code == 409 and stale.json()["code"] == 40978
    invalid = await client.patch(path + "/layout", json={
        "expected_revision": after["revision"],
        "nodes": [{"id": str(n.id), "x": 0}, {"id": str(uuid4()), "x": 1}],
    })
    assert invalid.status_code == 404
    assert (await client.get(path)).json()["data"] == after
    forbidden = await client.patch(path + "/layout", json={
        "expected_revision": after["revision"], "nodes": [{"id": str(n.id), "content": {"text": "Overwrite"}}],
    })
    assert forbidden.status_code == 422
    only_viewport = await client.patch(path + "/layout", json={
        "expected_revision": after["revision"], "viewport": {"x": 1, "y": 2, "zoom": 1},
    })
    assert only_viewport.status_code == 200 and only_viewport.json()["data"]["nodes"] == []


async def test_concurrent_layout_changes_allow_only_one_revision(ctx):
    n, _ = await setup_node(ctx)
    await ctx.db.rollback()

    async def move(x):
        async with ctx.factory() as db:
            try:
                return await canvases.patch_layout(db, ctx.pid, ctx.uid, ctx.cid, CanvasLayoutPatch(
                    expected_revision=2, nodes=[{"id": n.id, "x": x}],
                ))
            except AppException as exc:
                return exc.code

    results = await asyncio.gather(move(10), move(20))
    assert sum(isinstance(value, dict) for value in results) == 1
    assert 40978 in results
    saved = await canvases.get_canvas(ctx.db, ctx.pid, ctx.uid, ctx.cid)
    assert saved.revision == 3 and saved.nodes[0].content_revision == 1


async def test_canvas_tasks_restore_all_active_attempts_and_filter_without_snapshots(ctx, client):
    n, _ = await setup_node(ctx, kind="text")
    records = [await submit(ctx, n) for _ in range(4)]
    for record, status in zip(records, ["pending", "running", "success", "failed"]):
        task = await ctx.db.get(UserTaskRecord, UUID(record["task_record_id"]))
        task.status = status
        task.extra = {**task.extra, "next_poll_seconds": 17}
    await ctx.db.commit()
    other_canvas = await canvases.create_canvas(ctx.db, ctx.pid, ctx.uid, CanvasCreate(name="Other"))
    other = SimpleNamespace(**{**vars(ctx), "cid": other_canvas.id})
    other_node = n.model_copy(update={"id": uuid4()})
    await patch(other, upsert_nodes=[other_node])
    await submit(other, other_node)
    path = canvas_path(ctx) + "/tasks"
    active = await client.get(path)
    assert active.status_code == 200 and active.headers["cache-control"] == "no-store"
    data = active.json()["data"]
    assert data["total"] == 2
    assert {item["task_record_id"] for item in data["items"]} == {r["task_record_id"] for r in records[:2]}
    assert all(item["next_poll_seconds"] == 1 and not item["stop_polling"] for item in data["items"])
    assert all(not {"snapshot", "prompt", "extra", "result"} & item.keys() for item in data["items"])
    all_items = (await client.get(path, params={"status": "all", "node_id": str(n.id)})).json()["data"]
    assert all_items["total"] == 4
    for status in ("pending", "running", "success", "failed"):
        row = (await client.get(path, params={"status": status})).json()["data"]["items"][0]
        assert row["status"] == status
        if status in {"success", "failed"}:
            assert row["stop_polling"] and row["next_poll_seconds"] is None
    pages = [(await client.get(path, params={"page": i, "page_size": 1})).json()["data"] for i in (1, 2)]
    assert all(page["total"] == 2 and len(page["items"]) == 1 for page in pages)
    assert pages[0]["items"][0]["task_record_id"] != pages[1]["items"][0]["task_record_id"]
    assert (await client.get(path, params={"node_id": str(uuid4())})).json()["data"]["items"] == []
    assert (await client.get(path, params={"status": "invalid"})).status_code == 422
    assert (await client.get(path, params={"page_size": 101})).status_code == 422
    canvas = await canvases.get_canvas(ctx.db, ctx.pid, ctx.uid, ctx.cid)
    await patch(ctx, canvas.revision, delete_node_ids=[n.id])
    assert (await client.get(path)).json()["data"]["total"] == 2


async def test_batch_media_preserves_order_and_hides_foreign_resources(ctx, client):
    first, second = await picture(ctx), await picture(ctx)
    second.media_type, second.source_verified = "video", False
    other_project = Project(user_id=ctx.uid, name="Other", cover="", description="")
    ctx.db.add(other_project)
    await ctx.db.flush()
    foreign = ProjectMedia(project_id=other_project.id, source_key="test", source_type="upload", upload={"url": "https://example.com/private.png", "filename": "private"})
    ctx.db.add(foreign)
    await ctx.db.commit()
    missing = uuid4()
    path = f"/api/v1/projects/{ctx.pid}/media/batch"
    response = await client.post(path, json={"ids": [str(second.id), str(first.id), str(second.id), str(foreign.id), str(missing)]})
    assert response.status_code == 200
    data = response.json()["data"]
    assert [row["id"] for row in data["items"]] == [str(second.id), str(first.id)]
    assert data["items"][0]["media_type"] == "video" and not data["items"][0]["source_verified"]
    assert data["missing_ids"] == [str(foreign.id), str(missing)] and data["total"] == 2
    for body in ({"ids": []}, {"ids": [str(first.id)] * 101}, {"ids": [str(first.id)], "url": "https://example.com"}):
        assert (await client.post(path, json=body)).status_code == 422
    for table in (UserTaskRecord, TaskDispatchOutbox):
        assert await ctx.db.scalar(select(func.count()).select_from(table)) == 0


@pytest.mark.parametrize("case", ["foreign_owner", "agent", "disabled", "wrong_canvas"])
async def test_new_canvas_endpoints_enforce_ownership_and_kind(ctx, client, case):
    n, _ = await setup_node(ctx)
    row = await picture(ctx)
    if case == "foreign_owner":
        client.test_app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=uuid4())
    elif case in {"agent", "disabled"}:
        project = await ctx.db.get(Project, ctx.pid)
        if case == "agent":
            project.project_kind = "agent"
        else:
            project.is_enabled = False
        await ctx.db.commit()
    path = canvas_path(ctx) if case != "wrong_canvas" else f"/api/v1/projects/{uuid4()}/canvases/{ctx.cid}"
    assert (await client.get(path + "/tasks")).status_code == 404
    assert (await client.patch(path + "/layout", json={"expected_revision": 2, "nodes": [{"id": str(n.id), "x": 8}]})).status_code == 404
    pid = ctx.pid if case != "wrong_canvas" else uuid4()
    assert (await client.post(f"/api/v1/projects/{pid}/media/batch", json={"ids": [str(row.id)]})).status_code == 404
