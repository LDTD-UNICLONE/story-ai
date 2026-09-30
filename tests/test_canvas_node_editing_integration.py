# ruff: noqa: F811
import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app.models.canvas_generation import CanvasGeneration
from app.models.project_canvas import CanvasNode, ProjectCanvas
from app.models.task_record import UserTaskRecord
from app.models.points import UserPointsTransaction
from app.models.task_dispatch import TaskDispatchOutbox
from tests.test_canvas_api_improvements_integration import client, canvas_path  # noqa: F401
from tests.test_canvas_generations_integration import ctx, setup_node, submit, picture  # noqa: F401
from tests.test_project_canvases_integration import canvas_ctx, node, edge, patch  # noqa: F401
from tests.test_task_lifecycle_integration import lifecycle_db  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.skipif(os.getenv("RUN_DB_INTEGRATION_TESTS") != "1", reason="isolated PostgreSQL"),
]


async def test_partial_edit_preserves_model_media_and_generation_pointers(ctx, client):
    n, model = await setup_node(ctx, parameters={"n": 1})
    media = await picture(ctx)
    n.media_id = media.id
    n.x = 123
    await patch(ctx, 2, upsert_nodes=[n])
    record = await submit(ctx, n, revision=2)
    stored = await ctx.db.get(CanvasNode, (ctx.cid, n.id))
    stored.selected_generation_id = UUID(record["id"])
    await ctx.db.commit()
    path = canvas_path(ctx)
    before = (await client.get(path)).json()["data"]
    renamed = await client.patch(path, json={
        "expected_revision": before["revision"], "update_nodes": [{"id": str(n.id), "title": "New title"}],
    })
    assert renamed.status_code == 200
    saved = renamed.json()["data"]
    assert saved["nodes"] == [{**before["nodes"][0], "title": "New title"}]
    edited = await client.patch(path, json={
        "expected_revision": saved["revision"],
        "update_nodes": [{"id": str(n.id), "content": {"text": "Only change the prompt"}}],
    })
    assert edited.status_code == 200
    changed = edited.json()["data"]
    expected = {**saved["nodes"][0], "content_revision": 3,
                "content": {**saved["nodes"][0]["content"], "text": "Only change the prompt"}}
    assert changed["nodes"] == [expected]
    assert expected["content"]["generation"]["ai_model_id"] == str(model.id)
    assert expected["content"]["generation"]["parameters"]["n"] == 1
    cleared = await client.patch(path, json={
        "expected_revision": changed["revision"],
        "update_nodes": [{"id": str(n.id), "media_id": None, "content": {"generation": None}}],
    })
    assert cleared.status_code == 200
    result = cleared.json()["data"]["nodes"][0]
    assert result["media_id"] is None and result["selected_generation_id"] is None
    assert result["latest_generation_id"] == record["id"]
    assert result["content"] == {"text": "Only change the prompt", "generation": None}
    assert result["content_revision"] == 4
    for table in (CanvasGeneration, UserTaskRecord, UserPointsTransaction, TaskDispatchOutbox):
        assert await ctx.db.scalar(select(func.count()).select_from(table)) == 1


@pytest.mark.parametrize("case,status", [
    ("missing", 404), ("bad_parent", 400), ("bad_media", 400), ("stale", 409),
])
async def test_partial_edit_is_atomic_and_validates_graph(ctx, client, case, status):
    n, _ = await setup_node(ctx)
    path = canvas_path(ctx)
    before = (await client.get(path)).json()["data"]
    change = {"id": str(n.id), "title": "Must not persist"}
    updates = [change]
    if case == "missing":
        updates.append({"id": str(uuid4()), "title": "Missing"})
    elif case == "bad_parent":
        change["parent_id"] = str(n.id)
    elif case == "bad_media":
        change["media_id"] = str(uuid4())
    response = await client.patch(path, json={
        "expected_revision": before["revision"] - (case == "stale"), "update_nodes": updates,
        "name": "Must not rename canvas",
    })
    assert response.status_code == status
    await ctx.db.commit()
    assert (await client.get(path)).json()["data"] == before


async def test_partial_edit_and_edge_changes_share_one_transaction(ctx, client):
    source, _ = await setup_node(ctx)
    target, group = node("video"), node("group")
    link = edge(source, target)
    await patch(ctx, 2, upsert_nodes=[target, group], upsert_edges=[link])
    media = await picture(ctx)
    response = await client.patch(canvas_path(ctx), json={
        "expected_revision": 3,
        "update_nodes": [{"id": str(source.id), "media_id": str(media.id), "parent_id": str(group.id)}],
        "upsert_edges": [{**link.model_dump(mode="json"), "media_id": None}],
    })
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["edges"][0]["media_id"] == str(media.id)
    assert {n["id"]: n["content_revision"] for n in data["nodes"]} == {
        str(source.id): 2, str(target.id): 2, str(group.id): 1,
    }
    deleted = await client.patch(canvas_path(ctx), json={
        "expected_revision": data["revision"], "delete_node_ids": [str(group.id)],
    })
    remaining = {n["id"]: n for n in deleted.json()["data"]["nodes"]}
    assert remaining[str(source.id)]["parent_id"] is None
    assert remaining[str(source.id)]["media_id"] == str(media.id)
    assert remaining[str(source.id)]["content_revision"] == 2


async def test_canvas_delete_http_conflict_and_retained_task_history(ctx, client):
    n, _ = await setup_node(ctx)
    record = await submit(ctx, n)
    path = canvas_path(ctx)
    canvas = (await client.get(path)).json()["data"]
    missing_version = await client.delete(path)
    assert missing_version.status_code == 422
    conflict = await client.delete(path, params={"expected_revision": canvas["revision"] - 1})
    assert conflict.status_code == 409 and conflict.json()["code"] == 40978
    assert (await client.get(path)).json()["data"] == canvas
    deleted = await client.delete(path, params={"expected_revision": canvas["revision"]})
    assert deleted.status_code == 200
    assert deleted.json()["data"] == {"id": str(ctx.cid), "deleted": True}
    assert (await client.get(path)).status_code == 404
    assert (await client.get(path + "/tasks")).status_code == 404
    assert (await client.delete(path, params={"expected_revision": canvas["revision"]})).status_code == 404
    history = await client.get(path + f"/nodes/{n.id}/generations")
    assert history.status_code == 200 and history.json()["data"]["items"][0]["id"] == record["id"]
    task = await ctx.db.get(UserTaskRecord, UUID(record["task_record_id"]))
    assert task.status == "pending"
    assert await ctx.db.scalar(select(func.count()).select_from(UserPointsTransaction)) == 1
    assert await ctx.db.scalar(select(func.count()).select_from(ProjectCanvas)) == 0
    assert await ctx.db.scalar(select(func.count()).select_from(CanvasNode)) == 0
