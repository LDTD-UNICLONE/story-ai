from types import SimpleNamespace

import pytest

from app.core.timezone import beijing_datetime
from app.services.task_execution import TaskExecutionDeferred, prepare_task_execution
from app.worker import celery_app


def task(status: str, extra=None):
    return SimpleNamespace(status=status, extra=extra or {})


def test_pending_task_gets_execution_lease() -> None:
    record = task("pending")

    assert prepare_task_execution(record) is True
    assert record.extra["execution_attempt"] == 1
    assert record.extra["execution_lease_until"]


def test_running_task_with_active_lease_is_deferred() -> None:
    record = task(
        "running",
        {"execution_lease_until": beijing_datetime().replace(year=2099).isoformat()},
    )

    with pytest.raises(TaskExecutionDeferred):
        prepare_task_execution(record)


def test_running_task_with_expired_lease_is_reclaimed() -> None:
    record = task(
        "running",
        {
            "execution_attempt": 1,
            "execution_lease_until": beijing_datetime().replace(year=2000).isoformat(),
        },
    )

    assert prepare_task_execution(record) is True
    assert record.extra["execution_attempt"] == 2


def test_running_provider_task_is_not_submitted_again() -> None:
    record = task(
        "running",
        {"assistant_message_extra": {"task_id": "provider-123"}},
    )

    assert prepare_task_execution(record) is False


def test_running_provider_task_column_is_not_submitted_again() -> None:
    record = task("running")
    record.provider_task_id = "provider-column-123"

    assert prepare_task_execution(record) is False


def test_successful_conversation_points_have_periodic_recovery() -> None:
    schedule = celery_app.conf.beat_schedule["settle-pending-conversation-points"]

    assert schedule["task"] == "tasks.model_generation.settle_pending_conversation_points"
    assert celery_app.conf.task_routes[schedule["task"]]["queue"] == "story_ai_default"
