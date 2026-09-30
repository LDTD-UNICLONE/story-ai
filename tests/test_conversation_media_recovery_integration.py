from tests.provider_runtime import isolated_provider_pipeline, recover_and_transfer  # noqa: F401
# ruff: noqa: F811
import os
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.core.timezone import beijing_datetime
from app.integrations import apimart
from app.models.conversation import ConversationMessage
from app.models.points import UserPointsTransaction
from app.models.task_record import UserTaskRecord
from app.models.user import User
from app.services.generation import provider_reconciliation as recovery
from app.services.generation.runner import ModelRunResult
from app.tasks import model_generation as worker, provider_reconcile
from tests.test_task_lifecycle_integration import _seed_task, lifecycle_db  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_DB_INTEGRATION_TESTS") != "1", reason="isolated PostgreSQL"),
]


async def pending_task(sessions, kind):
    seed = await _seed_task(sessions, kind)
    async with sessions() as db:
        task = await db.get(UserTaskRecord, seed.task_id)
        task.status = "pending"
        task.provider_task_id = None
        task.extra = {**task.extra, "user_message_extra": task.extra["model_extra"]}
        await db.commit()
    return seed


async def make_due(sessions, task_id):
    async with sessions() as db:
        task = await db.get(UserTaskRecord, task_id)
        task.next_reconcile_at = beijing_datetime() - timedelta(seconds=1)
        await db.commit()


@pytest.mark.parametrize("kind", ["image", "video"])
async def test_worker_hands_off_without_polling_and_recovers_after_queue_outage(
    lifecycle_db, monkeypatch, kind,
):
    seed = await pending_task(lifecycle_db, kind)
    run = AsyncMock(return_value=ModelRunResult("accepted", {"task_id": "accepted-job", "task_status": "running"}))
    direct_query = AsyncMock(side_effect=AssertionError("Submission worker must not poll"))
    persist = AsyncMock(side_effect=AssertionError("No completed media to persist yet"))
    monkeypatch.setattr(worker, "WorkerSessionLocal", lifecycle_db)
    monkeypatch.setattr(worker, "run_model", run)
    monkeypatch.setattr(worker, "persist_generated_media_to_oss", persist)
    monkeypatch.setattr(apimart, "query_generation_task", direct_query)

    def unavailable(*args, **kwargs):
        raise ConnectionError("test broker unavailable")

    monkeypatch.setattr(provider_reconcile, "enqueue_provider_reconcile", unavailable)
    await worker._execute_generation(seed.task_id, seed.message_id)
    async with lifecycle_db() as db:
        task = await db.get(UserTaskRecord, seed.task_id)
        assert task.status == "running" and task.provider_task_id == "accepted-job"
        message = await db.get(ConversationMessage, seed.message_id)
        assert message.extra["task_status"] == "running"
        assert message.extra["next_poll_seconds"] > 0
        assert await db.scalar(select(func.count()).select_from(UserPointsTransaction)) == 0
    # Redelivery and a timeout handler must not submit again or refund an accepted job.
    for status in ("running", "pending"):
        async with lifecycle_db() as db:
            task = await db.get(UserTaskRecord, seed.task_id)
            task.status = status
            await db.commit()
        await worker._execute_generation(seed.task_id, seed.message_id)
        await worker._fail_generation(seed.task_id, seed.message_id, "timeout")
    run.assert_awaited_once()
    direct_query.assert_not_awaited()
    persist.assert_not_awaited()

    await make_due(lifecycle_db, seed.task_id)
    async with lifecycle_db() as db:
        candidates = await recovery.list_provider_reconcile_candidates(db)
        assert seed.task_id in [item.id for item in candidates]
    query = AsyncMock(return_value=ModelRunResult("https://example.com/result.png", {
        "task_id": "accepted-job", "task_status": "success", "credits_cost": "1",
    }))

    async def store(_kind, result):
        return result

    monkeypatch.setattr(recovery, "query_model_task", query)
    monkeypatch.setattr(recovery, "persist_generated_media_to_oss", store)
    for _ in range(2):
        async with lifecycle_db() as db:
            await recover_and_transfer(db, seed.task_id)
    query.assert_awaited_once()
    async with lifecycle_db() as db:
        task = await db.get(UserTaskRecord, seed.task_id)
        message = await db.get(ConversationMessage, seed.message_id)
        assert task.status == "success" and task.extra["points_settled"] is True
        assert message.content == task.result and message.extra["task_status"] == "success"
        assert await db.scalar(select(func.count()).select_from(UserPointsTransaction)) == 1
        assert (await db.get(User, seed.user_id)).points_balance == 94


@pytest.mark.parametrize("kind", ["image", "video"])
@pytest.mark.parametrize("failure", ["query", "storage", "provider"])
async def test_handed_off_task_only_refunds_on_provider_failure(lifecycle_db, monkeypatch, kind, failure):
    seed = await pending_task(lifecycle_db, kind)
    run = AsyncMock(return_value=ModelRunResult("accepted", {"provider_task_id": "accepted-job"}))
    monkeypatch.setattr(worker, "WorkerSessionLocal", lifecycle_db)
    monkeypatch.setattr(worker, "run_model", run)
    monkeypatch.setattr(provider_reconcile, "enqueue_provider_reconcile", lambda *a, **kw: None)
    await worker._execute_generation(seed.task_id, seed.message_id)
    await make_due(lifecycle_db, seed.task_id)
    query = AsyncMock(return_value=ModelRunResult("https://example.com/result.png", {
        "task_id": "accepted-job", "task_status": "failed" if failure == "provider" else "success",
    }))
    persist = AsyncMock(side_effect=ConnectionError("storage unavailable"))
    if failure == "query":
        query.side_effect = ConnectionError("query unavailable")
    monkeypatch.setattr(recovery, "query_model_task", query)
    monkeypatch.setattr(recovery, "persist_generated_media_to_oss", persist)
    async with lifecycle_db() as db:
        await recover_and_transfer(db, seed.task_id)
    await worker._execute_generation(seed.task_id, seed.message_id)
    await worker._fail_generation(seed.task_id, seed.message_id, "late failure")
    async with lifecycle_db() as db:
        # A repeated recovery call must respect terminal state or the retry deadline.
        await recover_and_transfer(db, seed.task_id)
        task = await db.get(UserTaskRecord, seed.task_id)
        expected = "failed" if failure == "provider" else "running"
        assert task.status == expected
        assert task.provider_task_id == "accepted-job"
        assert await db.scalar(select(func.count()).select_from(UserPointsTransaction)) == (failure == "provider")
        assert (await db.get(User, seed.user_id)).points_balance == (100 if failure == "provider" else 90)
        assert (await db.get(ConversationMessage, seed.message_id)).extra["task_status"] == expected
    run.assert_awaited_once()
    query.assert_awaited_once()
    if failure != "storage":
        persist.assert_not_awaited()


async def test_immediate_media_result_still_finishes_in_worker(lifecycle_db, monkeypatch):
    seed = await pending_task(lifecycle_db, "image")
    run = AsyncMock(return_value=ModelRunResult("https://example.com/ready.png", {"credits_cost": "1"}))
    monkeypatch.setattr(worker, "WorkerSessionLocal", lifecycle_db)
    monkeypatch.setattr(worker, "run_model", run)

    async def store(_kind, result):
        return result

    monkeypatch.setattr(worker, "persist_generated_media_to_oss", store)
    queued = []
    monkeypatch.setattr(provider_reconcile, "enqueue_provider_reconcile", lambda *a, **kw: queued.append(a))
    await worker._execute_generation(seed.task_id, seed.message_id)
    await worker._execute_generation(seed.task_id, seed.message_id)
    run.assert_awaited_once()
    assert queued == []
    async with lifecycle_db() as db:
        task = await db.get(UserTaskRecord, seed.task_id)
        assert task.status == "success" and task.extra["points_settled"] is True
        assert (await db.get(ConversationMessage, seed.message_id)).content == task.result
