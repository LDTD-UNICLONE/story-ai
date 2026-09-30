from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.timezone import beijing_datetime
from app.models.task_record import UserTaskRecord
from app.services.generation import provider_state as provider_task_state
from app.services.generation.provider_reconciliation import _is_provider_success_result
from app.services.generation.runner import ModelRunResult
from app.services.generation import task_records
from app.services.generation.task_execution import prepare_task_execution
from app.tasks import (
    project_asset_generation,
    project_storyboard_image,
    project_storyboard_video,
    provider_reconcile,
)


@pytest.mark.asyncio
async def test_accepted_provider_task_is_not_expired_or_refunded(monkeypatch) -> None:
    record = SimpleNamespace(
        status="running",
        created_at=beijing_datetime() - timedelta(hours=2),
        extra={"model_result_extra": {"task_id": "provider-task-123"}},
    )
    stale_calls = []

    async def mark_stale(_db, _record):
        stale_calls.append(_record)

    class FakeDb:
        async def refresh(self, _record):
            return None

    monkeypatch.setattr(task_records, "_mark_stale_failed", mark_stale)

    expired = await task_records.expire_stale_task_record(FakeDb(), record)

    assert expired is False
    assert stale_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "task_module", [project_asset_generation, project_storyboard_image, project_storyboard_video]
)
async def test_post_accept_failure_does_not_fail_refund_or_resubmit_media(monkeypatch, task_module) -> None:
    task_record_id = uuid4()
    storyboard_id = uuid4()
    task_record = SimpleNamespace(
        id=task_record_id,
        status="running",
        extra={"model_result_extra": {"task_id": "provider-task-456"}},
    )
    storyboard = SimpleNamespace(id=storyboard_id)
    failed_calls = []

    class FakeDb:
        async def get(self, model, record_id):
            if model is UserTaskRecord and record_id == task_record_id:
                return task_record
            if record_id == storyboard_id:
                return storyboard
            return None

    class SessionContext:
        async def __aenter__(self):
            return FakeDb()

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return None

    async def mark_failed(*args, **kwargs):
        failed_calls.append((args, kwargs))

    monkeypatch.setattr(task_module, "WorkerSessionLocal", lambda: SessionContext())
    monkeypatch.setattr(task_module, "_mark_failed", mark_failed)

    args = [task_record_id, storyboard_id, "reconcile enqueue failed"]
    if task_module is project_asset_generation:
        args.insert(1, "character")
    await task_module._fail_generation(*args)

    assert task_record.status == "running"
    assert failed_calls == []
    assert prepare_task_execution(task_record) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task_module", "worker_name"),
    [
        (project_asset_generation, "run_asset_image_generation_in_worker"),
        (project_storyboard_image, "run_storyboard_image_generation_in_worker"),
        (project_storyboard_video, "run_storyboard_video_generation_in_worker"),
    ],
)
async def test_post_accept_processing_error_is_handed_to_reconciler(
    monkeypatch, task_module, worker_name
) -> None:
    task_record_id = uuid4()
    storyboard_id = uuid4()
    task_record = SimpleNamespace(
        id=task_record_id,
        user_id=uuid4(),
        status="pending",
        extra={},
        provider_task_id=None,
    )
    storyboard = SimpleNamespace(id=storyboard_id, extra={})
    failed_calls = []
    enqueued = []

    class FakeResult:
        def scalar_one_or_none(self):
            return task_record

    class FakeDb:
        async def execute(self, _statement):
            return FakeResult()

        async def get(self, _model, record_id):
            return storyboard if record_id == storyboard_id else None

        async def commit(self):
            return None

        async def rollback(self):
            return None

        async def refresh(self, _record):
            return None

    class SessionContext:
        async def __aenter__(self):
            return FakeDb()

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return None

    async def accepted_then_failed(_db, record, *_resource_args):
        record.provider_task_id = "provider-task-durable"
        record.extra = {"model_result_extra": {"task_id": "provider-task-durable"}}
        await _db.commit()
        raise ConnectionError("OSS temporarily unavailable")

    async def mark_failed(*args, **kwargs):
        failed_calls.append((args, kwargs))

    monkeypatch.setattr(task_module, "WorkerSessionLocal", lambda: SessionContext())
    monkeypatch.setattr(
        task_module,
        worker_name,
        accepted_then_failed,
    )
    monkeypatch.setattr(task_module, "_mark_failed", mark_failed)
    monkeypatch.setattr(
        task_module,
        "_enqueue_provider_reconcile_if_needed",
        lambda record: enqueued.append(record.id),
    )

    args = [task_record_id, storyboard_id]
    if task_module is project_asset_generation:
        args.insert(1, "character")
    await task_module._execute_generation(*args)

    assert task_record.status == "running"
    assert enqueued == [task_record_id]
    assert failed_calls == []


def test_reconcile_enqueue_failure_is_left_for_safety_sweep(monkeypatch) -> None:
    record = SimpleNamespace(id=uuid4())

    monkeypatch.setattr(
        provider_reconcile,
        "should_reconcile_provider_task",
        lambda _record: True,
    )

    def fail_enqueue(*_args, **_kwargs):
        raise ConnectionError("broker unavailable")

    monkeypatch.setattr(provider_reconcile, "enqueue_provider_reconcile", fail_enqueue)

    assert provider_reconcile.enqueue_provider_reconcile_best_effort(record) is False


def test_provider_task_state_is_promoted_to_reconcile_fields() -> None:
    record = SimpleNamespace(
        status="running",
        generation_type="storyboard_video",
        provider_task_id=None,
        provider_vendor=None,
        provider_status=None,
        provider_submitted_at=None,
        next_reconcile_at=None,
        extra={},
    )
    before = beijing_datetime()

    recorded = provider_task_state.record_provider_task_state(
        record,
        {
            "task_id": "provider-task-789",
            "task_status": "in_progress",
            "platform_task_status": "running",
            "provider_response": {"progress": 46},
        },
        provider_vendor="volcengine_ark",
    )

    assert recorded is True
    assert record.provider_task_id == "provider-task-789"
    assert record.provider_vendor == "volcengine_ark"
    assert record.provider_status == "in_progress"
    assert record.extra["progress_percent"] == 46
    assert provider_task_state.task_record_progress_percent(record) == 46
    assert record.provider_submitted_at >= before
    assert record.next_reconcile_at == record.provider_submitted_at


def test_successful_media_task_progress_is_100() -> None:
    record = SimpleNamespace(
        status="success",
        generation_type="video",
        extra={"progress_percent": 87},
    )

    assert provider_task_state.task_record_progress_percent(record) == 100


def test_terminal_provider_state_clears_next_reconcile_time() -> None:
    record = SimpleNamespace(
        status="running",
        generation_type="image",
        provider_task_id="provider-task-987",
        provider_vendor="comfly",
        provider_status="running",
        provider_submitted_at=beijing_datetime(),
        next_reconcile_at=beijing_datetime() + timedelta(minutes=1),
        extra={},
    )

    provider_task_state.record_provider_task_state(
        record,
        {"task_id": "provider-task-987", "task_status": "success"},
    )

    assert record.provider_status == "success"
    assert record.next_reconcile_at is None


def test_provider_task_id_column_is_enough_to_protect_task() -> None:
    record = SimpleNamespace(provider_task_id="provider-task-column", extra={})

    assert provider_task_state.has_provider_task_id(record) is True


@pytest.mark.parametrize("status", ["failed", "failure", "fail", "error", "cancelled", "canceled"])
def test_failed_provider_result_is_not_treated_as_media_to_persist(status):
    result = ModelRunResult("https://example.com/error-preview.png", {"task_status": status})
    assert _is_provider_success_result(result, status) is False
