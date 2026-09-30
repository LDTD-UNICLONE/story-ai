from tests.provider_runtime import isolated_provider_pipeline, recover_and_transfer  # noqa: F401
# ruff: noqa: F811
import importlib
import os
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi import Response
from sqlalchemy import func, select

from app.api.v1.endpoints import conversations
from app.models.project_generated_asset import ProjectGeneratedAsset
from app.models.task_record import UserTaskRecord
from app.models.user import User
from app.services.generation import provider_reconciliation as recovery
from app.services.generation.runner import ModelRunResult
from app.tasks import provider_reconcile
from tests.test_task_lifecycle_integration import _seed_task, lifecycle_db  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_DB_INTEGRATION_TESTS") != "1", reason="isolated PostgreSQL"),
]


@pytest.mark.parametrize("kind,service,worker", [
    ("asset_image_generate", "asset_generation", "project_asset_generation"),
    ("storyboard_image", "storyboard_images", "project_storyboard_image"),
    ("storyboard_video", "storyboard_videos", "project_storyboard_video"),
])
async def test_agent_submission_releases_worker_and_redelivery_only_reconciles(
    lifecycle_db, monkeypatch, kind, service, worker,
):
    seed = await _seed_task(lifecycle_db, kind)
    async with lifecycle_db() as db:
        task = await db.get(UserTaskRecord, seed.task_id)
        task.status = "pending"
        task.provider_task_id = None
        await db.commit()
    service = importlib.import_module(f"app.services.projects.{service}")
    worker = importlib.import_module(f"app.tasks.{worker}")
    run = AsyncMock(return_value=ModelRunResult("accepted", {"task_id": "accepted-job", "task_status": "running"}))
    persist = AsyncMock(side_effect=AssertionError("No finished media yet"))
    monkeypatch.setattr(service, "run_model", run)
    monkeypatch.setattr(service, "persist_generated_media_to_oss", persist)
    monkeypatch.setattr(worker, "WorkerSessionLocal", lifecycle_db)
    enqueued = []
    monkeypatch.setattr(provider_reconcile, "enqueue_provider_reconcile", lambda task_id, countdown: enqueued.append((task_id, countdown)))
    args = (seed.task_id, "character", seed.character_id) if kind == "asset_image_generate" else (seed.task_id, seed.storyboard_id)
    await worker._execute_generation(*args)
    await worker._execute_generation(*args)
    run.assert_awaited_once()
    persist.assert_not_awaited()
    assert enqueued == [(str(seed.task_id), 0)] * 2
    async with lifecycle_db() as db:
        task = await db.get(UserTaskRecord, seed.task_id)
        assert task.status == "running" and task.provider_task_id == "accepted-job"
        assert task.extra["model_result_extra"]["provider_polling_deferred"] is True
    monkeypatch.setattr(recovery, "query_model_task", AsyncMock(return_value=ModelRunResult("https://example.com/result.png", {"task_status": "success"})))
    monkeypatch.setattr(recovery, "persist_generated_media_to_oss", AsyncMock(side_effect=lambda kind, result: result))
    async with lifecycle_db() as db:
        task = await recover_and_transfer(db, seed.task_id)
        assert task.status == "success"
        assert await db.scalar(select(func.count()).select_from(ProjectGeneratedAsset)) == 1


@pytest.mark.parametrize("kind", ["image", "video", "asset_image_generate", "storyboard_image", "storyboard_video"])
async def test_pending_query_without_repeated_id_does_not_wait_for_claim_expiry(lifecycle_db, monkeypatch, kind):
    seed = await _seed_task(lifecycle_db, kind)
    monkeypatch.setattr(recovery, "query_model_task", AsyncMock(return_value=ModelRunResult("生成任务处理中", {"task_status": "running"})))
    async with lifecycle_db() as db:
        task = await recover_and_transfer(db, seed.task_id)
        assert task.status == "running"
        assert task.provider_task_id == "provider-task"
        assert timedelta(0) < task.next_reconcile_at - task.last_reconcile_at <= timedelta(seconds=2.1)
        assert "provider_reconcile_claim_id" not in task.extra
        assert recovery.provider_reconcile_delay_seconds(task) <= 2


@pytest.mark.parametrize("status,expected", [("running", 1), ("success", None), ("failed", None)])
async def test_conversation_status_uses_common_hint_and_stops_at_terminal(lifecycle_db, status, expected):
    seed = await _seed_task(lifecycle_db, "image")
    async with lifecycle_db() as db:
        task = await db.get(UserTaskRecord, seed.task_id)
        task.status = status
        task.extra = {**task.extra, "next_poll_seconds": 120}
        await db.commit()
        response = Response()
        payload = await conversations.my_conversation_generation_task(
            conversation_id=task.business_id, task_record_id=task.id,
            response=response, wait_seconds=15, db=db,
            current_user=await db.get(User, seed.user_id),
        )
        assert payload.data["next_poll_seconds"] == expected
        assert response.headers.get("X-Next-Poll-Seconds") == ("1" if expected else None)
        assert payload.data["stop_polling"] == (status != "running")
