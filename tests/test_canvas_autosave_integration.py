from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from app.core.exceptions import AppException
from app.schemas.project_canvas import CanvasPatch
from app.services.generation.runner import ModelRunResult
from app.services.projects import canvases
from app.tasks import canvas_generation as worker
from tests import test_canvas_generations_integration as generation_fixtures
from tests import test_project_canvases_integration as canvas_fixtures
from tests import test_task_lifecycle_integration as lifecycle_fixtures

ctx = generation_fixtures.ctx
canvas_ctx = canvas_fixtures.canvas_ctx
lifecycle_db = lifecycle_fixtures.lifecycle_db
pytestmark = generation_fixtures.pytestmark


@pytest.mark.parametrize("kind", ["text", "image", "video"])
async def test_generation_is_saved_without_frontend_patch_and_returns_submission_revision(
    ctx, monkeypatch, kind
):
    node, _ = await generation_fixtures.setup_node(
        ctx, kind=kind, parameters={"duration": 5, "resolution": "720p"} if kind == "video" else {}
    )
    key = uuid4()
    submitted = await generation_fixtures.submit(ctx, node, key=key)
    assert submitted["revision"] == 3
    output = (
        "Generated text"
        if kind == "text"
        else f"https://example.com/result.{'png' if kind == 'image' else 'mp4'}"
    )
    run = AsyncMock(return_value=ModelRunResult(output, {}))
    monkeypatch.setattr(worker, "run_model", run)
    monkeypatch.setattr(
        worker, "persist_generated_media_to_oss", AsyncMock(side_effect=lambda kind, result: result)
    )
    await worker.execute_generation(UUID(submitted["task_record_id"]))

    # Simulate reopening the page: a new DB session sees the worker's committed canvas.
    async with ctx.factory() as db:
        saved = await canvases.get_canvas(db, ctx.pid, ctx.uid, ctx.cid)
    assert saved.revision == 4
    saved_node = next(item for item in saved.nodes if item.id == node.id)
    assert saved_node.selected_generation_id == UUID(submitted["id"])
    assert saved_node.content_revision == 2
    assert saved_node.content.text == "Generate a scene"
    task, history, _, _ = await generation_fixtures.result_for(ctx, submitted)
    assert task.status == "success"
    assert history.result["auto_applied"] is True
    if kind == "text":
        assert history.result["text"] == output
        assert saved_node.media_id is None
    else:
        assert str(saved_node.media_id) == history.result["media_ids"][0]

    # An old full-node save must not erase the result automatically attached by the worker.
    with pytest.raises(AppException) as error:
        await canvases.patch_canvas(
            ctx.db,
            ctx.pid,
            ctx.uid,
            ctx.cid,
            CanvasPatch(expected_revision=submitted["revision"], upsert_nodes=[node]),
        )
    assert error.value.code == 40978
    await ctx.db.rollback()
    merged = await canvases.patch_canvas(
        ctx.db,
        ctx.pid,
        ctx.uid,
        ctx.cid,
        CanvasPatch(
            expected_revision=saved.revision, update_nodes=[{"id": node.id, "title": "Local edit"}]
        ),
    )
    assert merged.revision == 5
    assert merged.nodes[0].selected_generation_id == saved_node.selected_generation_id
    assert merged.nodes[0].media_id == saved_node.media_id
    assert merged.nodes[0].content_revision == 2

    # Idempotent replays also report the observed current canvas revision, without a new task.
    replay = await generation_fixtures.submit(ctx, node, key=key)
    assert replay["id"] == submitted["id"]
    assert replay["revision"] == merged.revision
    assert (
        replay["content_revision"] == 1
    )  # Frozen generation input version, not live node version.
    assert run.await_count == 1
