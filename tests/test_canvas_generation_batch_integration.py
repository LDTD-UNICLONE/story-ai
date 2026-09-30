# ruff: noqa: F811
import os
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app.api.deps import get_current_user
from app.models.canvas_generation import CanvasGeneration
from app.models.points import UserPointsTransaction
from app.models.project import Project
from app.models.task_dispatch import TaskDispatchOutbox
from app.models.task_record import UserTaskRecord
from app.models.user import User
from app.schemas.canvas_generation import CanvasGenerationSelect
from app.services.projects import canvas_generations, canvases
from tests.test_canvas_api_improvements_integration import client, canvas_path  # noqa: F401
from tests.test_canvas_generations_integration import ctx, setup_node, submit  # noqa: F401
from tests.test_project_canvases_integration import canvas_ctx, patch  # noqa: F401
from tests.test_task_lifecycle_integration import lifecycle_db  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.skipif(os.getenv("RUN_DB_INTEGRATION_TESTS") != "1", reason="isolated PostgreSQL"),
]


async def test_batch_restores_selected_text_and_all_statuses_without_writes(ctx, client):
    n, _ = await setup_node(ctx, kind="text")
    records = [await submit(ctx, n) for _ in range(4)]
    result = {"text": "Selected older result", "urls": [], "media_ids": [], "auto_applied": False}
    for record, status in zip(records, ["success", "failed", "pending", "running"]):
        task = await ctx.db.get(UserTaskRecord, UUID(record["task_record_id"]))
        task.status = status
        if status == "failed":
            task.extra = {**task.extra, "failed_reason": "Generation failed"}
        generation = await ctx.db.get(CanvasGeneration, UUID(record["id"]))
        generation.result = result if status == "success" else {}
    await ctx.db.commit()
    await canvas_generations.select_generation(
        ctx.db, ctx.pid, ctx.uid, ctx.cid, n.id, UUID(records[0]["id"]),
        CanvasGenerationSelect(expected_content_revision=1),
    )
    before = (await client.get(canvas_path(ctx))).json()["data"]
    assert before["nodes"][0]["selected_generation_id"] == records[0]["id"]
    assert before["nodes"][0]["latest_generation_id"] is None
    tables = (CanvasGeneration, UserTaskRecord, UserPointsTransaction, TaskDispatchOutbox)
    counts = [await ctx.db.scalar(select(func.count()).select_from(table)) for table in tables]
    balance = (await ctx.db.get(User, ctx.uid)).points_balance
    ids = [r["id"] for r in reversed(records)]
    response = await client.post(canvas_path(ctx) + "/generations/batch", json={"ids": ids + ids})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    data = response.json()["data"]
    assert data["total"] == 4 and data["missing_ids"] == []
    assert [r["id"] for r in data["items"]] == ids
    assert [r["status"] for r in data["items"]] == ["running", "pending", "failed", "success"]
    assert data["items"][-1]["result"] == result
    assert data["items"][2]["failed_reason"] == "Generation failed"
    assert data["items"][0]["result"] == {}
    assert data["items"][-1]["content_revision"] == 1
    for item in data["items"]:
        assert set(item) == {
            "id", "canvas_id", "node_id", "task_record_id", "generation_type", "status",
            "content_revision", "points_cost", "result", "failed_reason", "created_at",
        }
    assert (await client.get(canvas_path(ctx))).json()["data"] == before
    assert [await ctx.db.scalar(select(func.count()).select_from(table)) for table in tables] == counts
    assert (await ctx.db.get(User, ctx.uid)).points_balance == balance
    await canvases.delete_canvas(ctx.db, ctx.pid, ctx.uid, ctx.cid, before["revision"])
    assert (await client.post(canvas_path(ctx) + "/generations/batch", json={"ids": ids})).json()["data"] == data


@pytest.mark.parametrize("case", [
    "other_canvas", "other_project", "task_owner", "task_business", "task_business_type",
])
async def test_batch_omits_out_of_scope_records(ctx, client, case):
    n, _ = await setup_node(ctx)
    record = await submit(ctx, n)
    generation = await ctx.db.get(CanvasGeneration, UUID(record["id"]))
    task = await ctx.db.get(UserTaskRecord, UUID(record["task_record_id"]))
    other_user = User(account=uuid4().hex, password_hash="test", nickname="Other")
    ctx.db.add(other_user)
    await ctx.db.flush()
    other_project = Project(user_id=other_user.id, name="Other", cover="", description="")
    ctx.db.add(other_project)
    await ctx.db.flush()
    if case == "other_canvas":
        generation.canvas_id = uuid4()
    elif case == "other_project":
        generation.project_id = other_project.id
    elif case == "task_owner":
        task.user_id = other_user.id
    elif case == "task_business":
        task.business_id = other_project.id
    else:
        task.business_type = "conversation"
    await ctx.db.commit()
    missing = str(uuid4())
    response = await client.post(canvas_path(ctx) + "/generations/batch", json={"ids": [record["id"], missing, record["id"]]})
    assert response.status_code == 200
    assert response.json()["data"] == {"items": [], "total": 0, "missing_ids": [record["id"], missing]}


@pytest.mark.parametrize("case", ["foreign_owner", "agent", "disabled", "missing_project"])
async def test_batch_requires_owned_enabled_standard_project(ctx, client, case):
    path = canvas_path(ctx) + "/generations/batch"
    if case == "foreign_owner":
        client.test_app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=uuid4())
    elif case == "missing_project":
        path = path.replace(str(ctx.pid), str(uuid4()))
    else:
        project = await ctx.db.get(Project, ctx.pid)
        if case == "agent":
            project.project_kind = "agent"
        else:
            project.is_enabled = False
        await ctx.db.commit()
    assert (await client.post(path, json={"ids": [str(uuid4())]})).status_code == 404


async def test_batch_validates_request_and_supports_single_or_maximum_batch(ctx, client):
    path = canvas_path(ctx) + "/generations/batch"
    for body in ({}, {"ids": []}, {"ids": [str(uuid4())] * 101}, {"ids": ["invalid"]},
                 {"ids": [str(uuid4())], "snapshot": True}, {"ids": None}):
        assert (await client.post(path, json=body)).status_code == 422
    for count in (1, 100):
        ids = [str(uuid4()) for _ in range(count)]
        response = await client.post(path, json={"ids": ids})
        assert response.status_code == 200
        assert response.json()["data"] == {"items": [], "total": 0, "missing_ids": ids}


async def test_batch_reads_multiple_nodes_and_media_candidates_in_one_request(ctx, client):
    records = []
    for kind in ("image", "video"):
        n, _ = await setup_node(ctx, kind=kind)
        record = await submit(ctx, n)
        generation = await ctx.db.get(CanvasGeneration, UUID(record["id"]))
        generation.result = {
            "text": None, "urls": [f"https://example.com/{kind}/1", f"https://example.com/{kind}/2"],
            "media_ids": [str(uuid4()), str(uuid4())], "auto_applied": False,
        }
        task = await ctx.db.get(UserTaskRecord, UUID(record["task_record_id"]))
        task.status = "success"
        await ctx.db.commit()
        records.append((record, generation.result, kind))
    missing = str(uuid4())
    data = (await client.post(canvas_path(ctx) + "/generations/batch", json={
        "ids": [records[1][0]["id"], missing, records[0][0]["id"]],
    })).json()["data"]
    assert data["total"] == 2 and data["missing_ids"] == [missing]
    for item, (record, result, kind) in zip(data["items"], reversed(records)):
        assert item["node_id"] == record["node_id"]
        assert item["generation_type"] == kind
        assert item["result"] == result
        assert item["failed_reason"] is None
    empty = await client.post(canvas_path(ctx).replace(str(ctx.cid), str(uuid4())) + "/generations/batch", json={
        "ids": [records[0][0]["id"]],
    })
    assert empty.status_code == 200
    assert empty.json()["data"] == {"items": [], "total": 0, "missing_ids": [records[0][0]["id"]]}
