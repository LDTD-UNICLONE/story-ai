# ruff: noqa: F811
import os
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.core.timezone import beijing_datetime
from app.models.task_dispatch import TaskDispatchOutbox
from app.models.task_record import UserTaskRecord
from app.models.points import UserPointsTransaction
from app.services.generation import provider_reconciliation as recovery
from app.services.generation.provider_budget import ProviderQueryDeferred
from app.services.generation.runner import ModelRunResult
from tests.provider_runtime import isolated_provider_pipeline  # noqa: F401
from tests.test_task_lifecycle_integration import lifecycle_db, _seed_task  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.skipif(os.getenv("RUN_DB_INTEGRATION_TESTS") != "1", reason="isolated database")]


async def due(sessions, task_id):
    async with sessions() as db:
        task = await db.get(UserTaskRecord, task_id)
        task.next_reconcile_at = beijing_datetime() - timedelta(seconds=1)
        await db.commit()


@pytest.mark.parametrize("kind", ["image", "video", "asset_image_generate", "storyboard_image", "storyboard_video"])
async def test_success_query_commits_result_and_one_outbox_without_downloading(lifecycle_db, monkeypatch, kind):
    seed = await _seed_task(lifecycle_db, kind)
    query = AsyncMock(return_value=ModelRunResult("https://example.com/result.png", {"task_status": "success", "credits_cost": "1"}))
    transfer = AsyncMock(side_effect=AssertionError("Query worker must not download"))
    monkeypatch.setattr(recovery, "query_model_task", query)
    monkeypatch.setattr(recovery, "persist_generated_media_to_oss", transfer)
    async with lifecycle_db() as db:
        task = await recovery.reconcile_provider_task_record(db, seed.task_id)
        assert task.status == "running" and task.extra["generation_phase"] == "persisting"
        assert task.extra["provider_completed_result"]["content"].endswith("result.png")
        assert await db.scalar(select(func.count()).select_from(TaskDispatchOutbox)) == 1
        assert await db.scalar(select(func.count()).select_from(UserPointsTransaction)) == 0
        assert await recovery.reconcile_provider_task_record(db, seed.task_id) is None
    transfer.assert_not_awaited()
    monkeypatch.setattr(recovery, "persist_generated_media_to_oss", AsyncMock(side_effect=lambda kind, result: result))
    async with lifecycle_db() as db:
        task = await recovery.transfer_provider_task_media(db, seed.task_id)
        assert task.status == "success"
        assert "provider_completed_result" not in task.extra
        assert await db.scalar(select(func.count()).select_from(UserPointsTransaction)) == 1
        assert await recovery.transfer_provider_task_media(db, seed.task_id) is None
    query.assert_awaited_once()


async def test_storage_failure_retries_saved_result_without_provider_query_or_refund(lifecycle_db, monkeypatch):
    seed = await _seed_task(lifecycle_db, "image")
    query = AsyncMock(return_value=ModelRunResult("https://example.com/result.png", {"task_status": "success", "credits_cost": "1"}))
    monkeypatch.setattr(recovery, "query_model_task", query)
    async with lifecycle_db() as db:
        await recovery.reconcile_provider_task_record(db, seed.task_id)
    monkeypatch.setattr(recovery, "persist_generated_media_to_oss", AsyncMock(side_effect=ConnectionError("storage offline")))
    async with lifecycle_db() as db:
        task = await recovery.transfer_provider_task_media(db, seed.task_id)
        assert task.status == "running" and task.extra["generation_phase"] == "persisting"
        assert task.extra["media_transfer_errors"] == 1
        assert task.next_reconcile_at > beijing_datetime()
        assert await db.scalar(select(func.count()).select_from(UserPointsTransaction)) == 0
    await due(lifecycle_db, seed.task_id)
    monkeypatch.setattr(recovery, "persist_generated_media_to_oss", AsyncMock(side_effect=lambda kind, result: result))
    async with lifecycle_db() as db:
        assert (await recovery.transfer_provider_task_media(db, seed.task_id)).status == "success"
    query.assert_awaited_once()


async def test_expired_transfer_claim_is_recovered_and_old_result_cannot_finalize(lifecycle_db, monkeypatch):
    seed = await _seed_task(lifecycle_db, "image")
    result = ModelRunResult("https://example.com/result.png", {"task_status": "success", "credits_cost": "1"})
    monkeypatch.setattr(recovery, "query_model_task", AsyncMock(return_value=result))
    async with lifecycle_db() as db:
        await recovery.reconcile_provider_task_record(db, seed.task_id)
        old = await recovery._claim_provider_reconcile(db, seed.task_id, transfer=True)
        assert await recovery._claim_provider_reconcile(db, seed.task_id, transfer=True) is None
    async with lifecycle_db() as db:
        task = await db.get(UserTaskRecord, seed.task_id)
        past = beijing_datetime() - timedelta(seconds=1)
        task.next_reconcile_at = past
        task.extra = {**task.extra, "provider_reconcile_claim_until": past.isoformat()}
        await db.commit()
        candidates = await recovery.list_provider_reconcile_candidates(db)
        assert seed.task_id in [row.id for row in candidates]
        new = await recovery._claim_provider_reconcile(db, seed.task_id, transfer=True)
        assert await recovery._finish_provider_reconcile_claim(db, old, model_result=result, query_failed=False, transfer=True) is None
        task = await recovery._finish_provider_reconcile_claim(db, new, model_result=result, query_failed=False, transfer=True)
        assert task.status == "success"
        assert await db.scalar(select(func.count()).select_from(UserPointsTransaction)) == 1


async def test_budget_deferral_does_not_contact_provider_or_count_as_error(lifecycle_db, monkeypatch):
    from contextlib import asynccontextmanager
    seed = await _seed_task(lifecycle_db, "image")
    @asynccontextmanager
    async def full(_vendor):
        raise ProviderQueryDeferred(5)
        yield
    query = AsyncMock()
    monkeypatch.setattr(recovery, "provider_query_slot", full)
    monkeypatch.setattr(recovery, "query_model_task", query)
    async with lifecycle_db() as db:
        task = await recovery.reconcile_provider_task_record(db, seed.task_id)
        assert task.status == "running" and task.extra["provider_query_errors"] == 0
        assert 4 <= recovery.provider_reconcile_delay_seconds(task) <= 5
    query.assert_not_awaited()
