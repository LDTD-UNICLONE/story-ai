"""Drive separate query/transfer jobs in business regression tests, without a broker."""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from app.services.generation import provider_reconciliation as recovery


@pytest.fixture(autouse=True)
def isolated_provider_pipeline(monkeypatch):
    @asynccontextmanager
    async def slot(_vendor):
        yield
    monkeypatch.setattr(recovery, "provider_query_slot", slot)
    monkeypatch.setattr(recovery, "dispatch_tasks_best_effort", AsyncMock())
    monkeypatch.setattr(recovery, "publish_task_change", AsyncMock())


async def recover_and_transfer(db, task_id):
    record = await recovery.reconcile_provider_task_record(db, task_id)
    if record is None:
        from app.models.task_record import UserTaskRecord
        candidate = await db.get(UserTaskRecord, task_id)
    else:
        candidate = record
    if candidate is not None and recovery.should_transfer_provider_media(candidate):
        transferred = await recovery.transfer_provider_task_media(db, task_id)
        return transferred if transferred is not None else record
    return record
