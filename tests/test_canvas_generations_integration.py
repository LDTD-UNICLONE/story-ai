from tests.provider_runtime import isolated_provider_pipeline, recover_and_transfer  # noqa: F401
# ruff: noqa: F811
import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from app.api.deps import get_current_user
from app.api.v1.endpoints.canvas_generations import router
from app.core.config import settings
from app.core.exceptions import AppException, register_exception_handlers
from app.db.session import get_db
from app.models.ai_model import AiModel
from app.models.canvas_generation import CanvasGeneration
from app.models.project_canvas import CanvasNode
from app.models.points import UserPointsTransaction
from app.models.project_media import ProjectMedia
from app.models.seedance_image import SeedanceImage
from app.models.task_dispatch import TaskDispatchOutbox
from app.models.task_record import UserTaskRecord
from app.models.user import User
from app.schemas.canvas_generation import CanvasGenerationSelect, CanvasGenerationSubmit
from app.services.generation import provider_reconciliation as reconciliation
from app.services.generation import submission
from app.services.generation.runner import ModelRunResult
from app.services.projects import canvas_generations as generations, canvases
from app.services.projects import canvas_results
from app.services.seedance_images import provider_scope
from app.tasks import canvas_generation as worker, provider_reconcile
from tests.test_project_canvases_integration import canvas_ctx, edge, node, patch  # noqa: F401
from tests.test_task_lifecycle_integration import lifecycle_db  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.skipif(os.getenv("RUN_DB_INTEGRATION_TESTS") != "1", reason="isolated PostgreSQL"),
]


@pytest.fixture
async def ctx(canvas_ctx, monkeypatch):
    ctx = canvas_ctx
    monkeypatch.setattr(generations, "dispatch_tasks_best_effort", AsyncMock())
    monkeypatch.setattr(worker, "WorkerSessionLocal", ctx.factory)
    monkeypatch.setattr(
        provider_reconcile, "enqueue_provider_reconcile_best_effort", lambda *args: None
    )
    monkeypatch.setattr(settings, "apimart_api_key", "canvas-test-key")
    user = await ctx.db.get(User, ctx.uid)
    user.points_balance = 100000
    await ctx.db.commit()
    return ctx


async def setup_node(
    ctx, kind="image", vendor="apimart", model_id=None, parameters=None, text="Generate a scene"
):
    model = AiModel(
        nickname="Canvas test",
        vendor=vendor,
        model_id=model_id
        or {"image": "gpt-image-2", "text": "gpt-4o", "video": "pixverse-v6"}[kind],
        model_type=kind,
        is_enabled=True,
        configuration={"billing": {"base_points": 6, "multipliers": {"platform": "1"}}},
    )
    ctx.db.add(model)
    await ctx.db.commit()
    n = node(
        kind,
        content={
            "text": text,
            "generation": {"ai_model_id": model.id, "parameters": parameters or {}},
        },
    )
    canvas = await canvases.get_canvas(ctx.db, ctx.pid, ctx.uid, ctx.cid)
    await patch(ctx, canvas.revision, upsert_nodes=[n])
    return n, model


async def picture(ctx):
    row = ProjectMedia(
        project_id=ctx.pid,
        source_key=uuid4().hex,
        source_type="upload",
        upload={"url": f"https://example.com/{uuid4()}.png", "filename": "image.png"},
    )
    ctx.db.add(row)
    await ctx.db.commit()
    return row


async def submit(ctx, n, revision=1, key=None):
    return await generations.submit_generation(
        ctx.db,
        ctx.pid,
        ctx.uid,
        ctx.cid,
        n.id,
        CanvasGenerationSubmit(expected_content_revision=revision, idempotency_key=key or uuid4()),
    )


async def result_for(ctx, result):
    async with ctx.factory() as db:
        task = await db.get(UserTaskRecord, UUID(result["task_record_id"]))
        generation = await db.get(CanvasGeneration, UUID(result["id"]))
        n = await db.get(CanvasNode, (ctx.cid, generation.node_id))
        user = await db.get(User, ctx.uid)
        return task, generation, n, user


async def test_concurrent_idempotency_one_charge_and_outbox(ctx):
    n, _ = await setup_node(ctx)
    key = uuid4()

    async def send():
        async with ctx.factory() as db:
            return await generations.submit_generation(
                db,
                ctx.pid,
                ctx.uid,
                ctx.cid,
                n.id,
                CanvasGenerationSubmit(expected_content_revision=1, idempotency_key=key),
            )

    a, b = await asyncio.gather(send(), send())
    assert a["id"] == b["id"]
    for table in (CanvasGeneration, UserTaskRecord, UserPointsTransaction, TaskDispatchOutbox):
        assert await ctx.db.scalar(select(func.count()).select_from(table)) == 1
    dispatch = await ctx.db.scalar(select(TaskDispatchOutbox))
    assert dispatch.queue == "story_ai_image"
    with pytest.raises(AppException) as error:
        await submit(ctx, n, 2, key)
    assert error.value.code == 40980


async def test_unverified_migrated_image_keeps_preview_but_cannot_review_or_generate(ctx, monkeypatch):
    from app.services.projects import media

    n, _ = await setup_node(ctx)
    row = await picture(ctx)
    row.source_verified = False
    await ctx.db.commit()
    source = node(media_id=row.id)
    await patch(ctx, 2, upsert_nodes=[source], upsert_edges=[edge(source, n)])
    inputs = await canvases.image_inputs(ctx.db, ctx.pid, ctx.uid, ctx.cid, n.id)
    assert not inputs["items"][0]["available"]
    assert inputs["items"][0]["media"]["url"] == row.upload["url"]
    download = AsyncMock(side_effect=AssertionError("Unverified media must never be downloaded"))
    monkeypatch.setattr(media, "register_stored_image", download)
    with pytest.raises(AppException) as review_error:
        await media.review_media(ctx.db, ctx.pid, ctx.uid, row.id)
    assert review_error.value.code == 40016
    with pytest.raises(AppException) as generation_error:
        await submit(ctx, n, revision=2)
    assert generation_error.value.code == 40073
    download.assert_not_awaited()
    for table in (CanvasGeneration, UserTaskRecord, UserPointsTransaction, TaskDispatchOutbox):
        assert await ctx.db.scalar(select(func.count()).select_from(table)) == 0


async def test_snapshot_and_retry_survive_node_and_model_edits(ctx):
    n, model = await setup_node(ctx)
    key = uuid4()
    result = await submit(ctx, n, key=key)
    n.content.text = "changed"
    await patch(ctx, 3, upsert_nodes=[n])
    model.model_id = "changed-model"
    await ctx.db.commit()
    repeated = await submit(ctx, n, key=key)
    assert repeated["id"] == result["id"]
    assert repeated["snapshot"]["prompt"] == "Generate a scene"
    assert repeated["snapshot"]["model"]["model_id"] == "gpt-image-2"
    with pytest.raises(AppException) as error:
        await submit(ctx, n)
    assert error.value.code == 40979


@pytest.mark.parametrize("kind", ["text", "image", "video"])
async def test_free_node_execution_and_worker_redelivery(ctx, monkeypatch, kind):
    from app.models.project import Project
    from app.models.style import Style
    from app.services import prompts

    def no_template(*args, **kwargs):
        pytest.fail("Canvas generation must not read built-in system templates")

    monkeypatch.setattr(prompts, "load_system_prompt", no_template)
    style = Style(name=str(uuid4()), cover="", prompt="Do not inject this project style")
    ctx.db.add(style)
    await ctx.db.flush()
    project = await ctx.db.get(Project, ctx.pid)
    project.style_id = style.id
    await ctx.db.commit()
    n, model = await setup_node(
        ctx, kind, parameters={"duration": 5, "resolution": "720p"} if kind == "video" else {}
    )
    result = await submit(ctx, n)
    content = (
        "Generated text"
        if kind == "text"
        else f"https://example.com/result.{'png' if kind == 'image' else 'mp4'}"
    )
    run = AsyncMock(return_value=ModelRunResult(content, {}))
    monkeypatch.setattr(worker, "run_model", run)
    monkeypatch.setattr(
        worker, "persist_generated_media_to_oss", AsyncMock(side_effect=lambda kind, result: result)
    )
    model.model_id = "edited-after-submission"
    await ctx.db.commit()
    task_id = UUID(result["task_record_id"])
    await worker.execute_generation(task_id)
    await worker.execute_generation(task_id)
    task, history, saved, user = await result_for(ctx, result)
    assert task.status == "success" and task.extra["points_settled"]
    assert saved.selected_generation_id == history.id and saved.content_revision == 2
    assert history.result["auto_applied"] and run.await_count == 1
    assert run.call_args.args[0].model_id != "edited-after-submission"
    assert run.call_args.args[2] == "Generate a scene"
    assert "system_prompt" not in run.call_args.args[3]
    assert "messages" not in run.call_args.args[3]
    assert run.call_args.kwargs["idempotency_key"] == str(task_id)
    if kind == "text":
        assert history.result["text"] == content and saved.media_id is None
    else:
        assert str(saved.media_id) == history.result["media_ids"][0]
        async with ctx.factory() as db:
            media = await db.get(ProjectMedia, saved.media_id)
            assert media.media_type == kind


@pytest.mark.parametrize(
    "change", ["edit", "partial_edit", "new_generation", "delete", "delete_canvas", "recreate"]
)
async def test_old_result_kept_in_history_without_overwrite(ctx, monkeypatch, change):
    n, _ = await setup_node(ctx)
    first = await submit(ctx, n)
    if change == "edit":
        n.content.text = "new prompt"
        await patch(ctx, 3, upsert_nodes=[n])
    elif change == "partial_edit":
        await patch(ctx, 3, update_nodes=[{"id": n.id, "content": {"text": "new prompt"}}])
    elif change == "new_generation":
        await submit(ctx, n)
    elif change == "delete_canvas":
        await canvases.delete_canvas(ctx.db, ctx.pid, ctx.uid, ctx.cid, 3)
    else:
        await patch(ctx, 3, delete_node_ids=[n.id])
        if change == "recreate":
            await patch(ctx, 4, upsert_nodes=[n])
    monkeypatch.setattr(
        worker, "run_model", AsyncMock(return_value=ModelRunResult("https://example.com/old.png"))
    )
    monkeypatch.setattr(
        worker, "persist_generated_media_to_oss", AsyncMock(side_effect=lambda kind, result: result)
    )
    await worker.execute_generation(UUID(first["task_record_id"]))
    task, history, saved, _ = await result_for(ctx, first)
    assert task.status == "success" and not history.result["auto_applied"]
    assert saved is None or saved.media_id is None
    listing = await generations.list_generations(ctx.db, ctx.pid, ctx.uid, ctx.cid, n.id, 1, 20)
    assert any(row["id"] == first["id"] for row in listing["items"])


async def test_explicit_history_selection_fences_later_task(ctx, monkeypatch):
    n, _ = await setup_node(ctx)
    first = await submit(ctx, n)
    monkeypatch.setattr(
        worker, "run_model", AsyncMock(return_value=ModelRunResult("https://example.com/first.png"))
    )
    monkeypatch.setattr(
        worker, "persist_generated_media_to_oss", AsyncMock(side_effect=lambda kind, result: result)
    )
    await worker.execute_generation(UUID(first["task_record_id"]))
    second = await submit(ctx, n, 2)
    selected = await generations.select_generation(
        ctx.db,
        ctx.pid,
        ctx.uid,
        ctx.cid,
        n.id,
        UUID(first["id"]),
        CanvasGenerationSelect(expected_content_revision=2),
    )
    await worker.execute_generation(UUID(second["task_record_id"]))
    _, history, saved, _ = await result_for(ctx, second)
    assert not history.result["auto_applied"] and saved.selected_generation_id == UUID(first["id"])
    assert str(saved.media_id) == selected["media_id"]


@pytest.mark.parametrize(
    "case",
    [
        "missing_reference",
        "empty_input",
        "unreviewed",
        "invalid_params",
        "wrong_model",
        "foreign_user",
    ],
)
async def test_invalid_submission_has_no_charge_task_or_dispatch(ctx, case):
    n, model = await setup_node(
        ctx, "video", model_id="seedance-2.0", parameters={"duration": 5, "resolution": "720p"}
    )
    if case == "missing_reference":
        n.content.text = f"use @{{{uuid4()}}}"
    elif case in {"empty_input", "unreviewed"}:
        media = await picture(ctx) if case == "unreviewed" else None
        source = node(media_id=media.id if media else None)
        await patch(ctx, 2, upsert_nodes=[source], upsert_edges=[edge(source, n)])
    elif case == "invalid_params":
        n.content.generation.parameters.resolution = "unsupported"
    elif case == "wrong_model":
        model.model_type = "image"
        await ctx.db.commit()
    if case in {"missing_reference", "invalid_params"}:
        await patch(ctx, 2, upsert_nodes=[n])
    canvas = await canvases.get_canvas(ctx.db, ctx.pid, ctx.uid, ctx.cid)
    rev = next(x.content_revision for x in canvas.nodes if x.id == n.id)
    with pytest.raises(AppException):
        await generations.submit_generation(
            ctx.db,
            ctx.pid,
            uuid4() if case == "foreign_user" else ctx.uid,
            ctx.cid,
            n.id,
            CanvasGenerationSubmit(expected_content_revision=rev, idempotency_key=uuid4()),
        )
    await ctx.db.commit()
    for table in (UserTaskRecord, CanvasGeneration, UserPointsTransaction, TaskDispatchOutbox):
        assert await ctx.db.scalar(select(func.count()).select_from(table)) == 0


async def test_seedance_pinned_reference_compilation_and_review_reuse(ctx):
    media = await picture(ctx)
    review = SeedanceImage(
        user_id=ctx.uid,
        provider_scope=provider_scope(),
        sha256=uuid4().hex,
        status="ready",
        asset_url="asset://approved",
        upload={
            **media.upload,
            "content_type": "image/png",
            "object_key": "image.png",
            "size": 100,
            "file_type": "image",
        },
    )
    ctx.db.add(review)
    await ctx.db.commit()
    n, _ = await setup_node(
        ctx, "video", model_id="seedance-2.0", parameters={"duration": 5, "resolution": "720p"}
    )
    source = node(media_id=media.id)
    link = edge(source, n)
    n.content.text = f"animate @{{{link.id}}}"
    await patch(ctx, 2, upsert_nodes=[source, n], upsert_edges=[link])
    result = await submit(ctx, n, 2)
    snapshot = result["snapshot"]
    assert snapshot["prompt"] == "animate @图片1"
    assert snapshot["model_extra"]["image_urls"] == ["asset://approved"]
    assert snapshot["inputs"][0]["media_id"] == str(media.id)
    assert snapshot["inputs"][0]["url"] == media.upload["url"]
    assert await ctx.db.scalar(select(func.count()).select_from(SeedanceImage)) == 1


async def test_failure_refunds_once(ctx):
    n, _ = await setup_node(ctx)
    result = await submit(ctx, n)
    await worker.fail_generation(UUID(result["task_record_id"]), "Test failure")
    await worker.fail_generation(UUID(result["task_record_id"]), "Test failure")
    task, _, saved, user = await result_for(ctx, result)
    assert task.status == "failed" and user.points_balance == 100000
    assert saved.selected_generation_id is None
    assert await ctx.db.scalar(select(func.count()).select_from(UserPointsTransaction)) == 2


async def test_async_provider_recovery_uses_frozen_model_and_no_resubmit(ctx, monkeypatch):
    n, model = await setup_node(ctx)
    result = await submit(ctx, n)
    task_id = UUID(result["task_record_id"])
    run = AsyncMock(
        return_value=ModelRunResult(
            "生成任务处理中", {"task_id": "accepted", "task_status": "running"}
        )
    )
    monkeypatch.setattr(worker, "run_model", run)
    await worker.execute_generation(task_id)
    await worker.execute_generation(task_id)
    model.model_id = "changed"
    await ctx.db.commit()
    query = AsyncMock(
        return_value=ModelRunResult("https://example.com/done.png", {"task_status": "success"})
    )
    monkeypatch.setattr(reconciliation, "query_model_task", query)
    monkeypatch.setattr(
        reconciliation,
        "persist_generated_media_to_oss",
        AsyncMock(side_effect=lambda kind, result: result),
    )
    async with ctx.factory() as db:
        record = await db.get(UserTaskRecord, task_id)
        record.next_reconcile_at = None
        await db.commit()
        await recover_and_transfer(db, task_id)
    task, history, saved, _ = await result_for(ctx, result)
    assert task.status == "success" and saved.selected_generation_id == history.id
    assert run.await_count == 1 and query.call_args.args[0].model_id == "gpt-image-2"


async def test_settlement_failure_preserves_result_and_beat_recovers(ctx, monkeypatch):
    n, _ = await setup_node(ctx)
    result = await submit(ctx, n)
    monkeypatch.setattr(
        worker, "run_model", AsyncMock(return_value=ModelRunResult("https://example.com/done.png"))
    )
    monkeypatch.setattr(
        worker, "persist_generated_media_to_oss", AsyncMock(side_effect=lambda kind, result: result)
    )
    original = canvas_results.settle_image_task_points
    monkeypatch.setattr(
        canvas_results,
        "settle_image_task_points",
        AsyncMock(side_effect=RuntimeError("temporary settlement error")),
    )
    await worker.execute_generation(UUID(result["task_record_id"]))
    task, history, _, _ = await result_for(ctx, result)
    assert task.status == "success" and history.result and not task.extra["points_settled"]
    monkeypatch.setattr(canvas_results, "settle_image_task_points", original)
    assert await worker._settle_pending(10) == 1
    assert (await result_for(ctx, result))[0].extra["points_settled"]


async def test_submission_rolls_back_charge_when_outbox_fails(ctx, monkeypatch):
    n, _ = await setup_node(ctx)
    monkeypatch.setattr(
        submission,
        "enqueue_task_dispatch",
        AsyncMock(side_effect=RuntimeError("outbox unavailable")),
    )
    with pytest.raises(RuntimeError):
        await submit(ctx, n)
    await ctx.db.commit()
    for table in (UserTaskRecord, CanvasGeneration, UserPointsTransaction):
        assert await ctx.db.scalar(select(func.count()).select_from(table)) == 0
    saved = await canvases.get_canvas(ctx.db, ctx.pid, ctx.uid, ctx.cid)
    assert saved.revision == 2 and saved.nodes[0].latest_generation_id is None


async def test_generation_http_contract(ctx):
    n, _ = await setup_node(ctx)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    register_exception_handlers(app)

    async def session():
        async with ctx.factory() as db:
            yield db

    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=ctx.uid)
    path = f"/api/v1/projects/{ctx.pid}/canvases/{ctx.cid}/nodes/{n.id}/generations"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        body = {"expected_content_revision": 1, "idempotency_key": str(uuid4())}
        first = await client.post(path, json=body)
        assert first.status_code == 200
        assert (await client.post(path, json=body)).json()["data"]["id"] == first.json()["data"][
            "id"
        ]
        assert (await client.get(path)).json()["data"]["total"] == 1
        assert (
            await client.post(path, json={**body, "image_urls": ["http://invalid"]})
        ).status_code == 422


async def test_storage_retry_uses_cached_provider_result(ctx, monkeypatch):
    n, _ = await setup_node(ctx)
    result = await submit(ctx, n)
    run = AsyncMock(return_value=ModelRunResult("https://example.com/result.png"))
    monkeypatch.setattr(worker, "run_model", run)
    persist = AsyncMock(
        side_effect=[
            RuntimeError("storage unavailable"),
            ModelRunResult("https://example.com/stored.png"),
        ]
    )
    monkeypatch.setattr(worker, "persist_generated_media_to_oss", persist)
    with pytest.raises(RuntimeError):
        await worker.execute_generation(UUID(result["task_record_id"]))
    await worker.execute_generation(UUID(result["task_record_id"]))
    assert run.await_count == 1 and (await result_for(ctx, result))[0].status == "success"


async def test_async_failure_and_repeated_failure_refund_only_once(ctx, monkeypatch):
    n, _ = await setup_node(ctx)
    result = await submit(ctx, n)
    task_id = UUID(result["task_record_id"])
    monkeypatch.setattr(
        worker,
        "run_model",
        AsyncMock(
            return_value=ModelRunResult(
                "running", {"task_id": "provider-job", "task_status": "running"}
            )
        ),
    )
    await worker.execute_generation(task_id)
    await worker.fail_generation(task_id, "must not refund accepted job")
    assert (await result_for(ctx, result))[0].status == "running"
    monkeypatch.setattr(
        reconciliation,
        "query_model_task",
        AsyncMock(return_value=ModelRunResult("failed", {"task_status": "failed"})),
    )
    async with ctx.factory() as db:
        record = await db.get(UserTaskRecord, task_id)
        record.next_reconcile_at = None
        await db.commit()
        await recover_and_transfer(db, task_id)
        await recover_and_transfer(db, task_id)
    task, history, saved, user = await result_for(ctx, result)
    assert task.status == "failed" and user.points_balance == 100000
    assert not history.result and saved.selected_generation_id is None
    assert await ctx.db.scalar(select(func.count()).select_from(UserPointsTransaction)) == 2


@pytest.mark.parametrize("mode", ["text_to_video", "reference", "first_last_frame"])
@pytest.mark.parametrize(
    "vendor,model_id",
    [
        ("apimart", "pixverse-v6"),
        ("comfly", "wan-2.1"),
        ("volcengine_ark", "doubao-seedance-2-0-260128"),
    ],
)
async def test_video_mode_compiles_through_actual_provider_validator(ctx, mode, vendor, model_id):
    n, _ = await setup_node(ctx, "video", vendor, model_id, {"duration": 5, "resolution": "720p"})
    rev = 1
    if mode != "text_to_video":
        media = await picture(ctx)
        source = node(media_id=media.id)
        link = edge(source, n, input="first_frame" if mode == "first_last_frame" else "reference")
        links = [link]
        if mode == "first_last_frame":
            links.append(edge(source, n, input="last_frame"))
        await patch(ctx, 2, upsert_nodes=[source], upsert_edges=links)
        rev = 2
    result = await submit(ctx, n, rev)
    assert result["snapshot"]["model_extra"]["generation_mode"] == mode


async def test_invalid_reference_mode_mix_and_unsupported_source_block_submission(ctx):
    n, _ = await setup_node(ctx, "video", parameters={"duration": 5, "resolution": "720p"})
    media = await picture(ctx)
    source = node(media_id=media.id)
    a, b = edge(source, n), edge(source, n, input="first_frame")
    await patch(ctx, 2, upsert_nodes=[source], upsert_edges=[a, b])
    with pytest.raises(AppException):
        await submit(ctx, n, 2)
    assert await ctx.db.scalar(select(func.count()).select_from(UserTaskRecord)) == 0


async def test_layout_does_not_change_content_version_or_prevent_apply(ctx, monkeypatch):
    n, _ = await setup_node(ctx)
    result = await submit(ctx, n)
    from app.schemas.project_canvas import CanvasLayoutPatch

    await canvases.patch_layout(ctx.db, ctx.pid, ctx.uid, ctx.cid, CanvasLayoutPatch(
        expected_revision=3, nodes=[{"id": n.id, "x": 500}],
    ))
    monkeypatch.setattr(
        worker,
        "run_model",
        AsyncMock(return_value=ModelRunResult("https://example.com/result.png")),
    )
    monkeypatch.setattr(
        worker, "persist_generated_media_to_oss", AsyncMock(side_effect=lambda kind, result: result)
    )
    await worker.execute_generation(UUID(result["task_record_id"]))
    _, history, saved, _ = await result_for(ctx, result)
    assert history.result["auto_applied"] and saved.x == 500


async def test_seedance_account_change_does_not_query_or_refund_accepted_job(ctx, monkeypatch):
    n, _ = await setup_node(ctx, "video", model_id="seedance-2.0", parameters={"duration": 5, "resolution": "720p"})
    result = await submit(ctx, n)
    task_id = UUID(result["task_record_id"])
    monkeypatch.setattr(worker, "run_model", AsyncMock(return_value=ModelRunResult("running", {"task_id": "seedance-job", "task_status": "running"})))
    await worker.execute_generation(task_id)
    query = AsyncMock()
    monkeypatch.setattr(reconciliation, "query_model_task", query)
    monkeypatch.setattr(settings, "apimart_api_key", "different-account")
    async with ctx.factory() as db:
        record = await db.get(UserTaskRecord, task_id)
        record.next_reconcile_at = None
        await db.commit()
        await recover_and_transfer(db, task_id)
    task, _, _, _ = await result_for(ctx, result)
    assert task.status == "running" and task.extra["reconcile_blocked_reason"]
    assert query.await_count == 0 and not task.extra.get("refund_transaction_id")
