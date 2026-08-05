from datetime import datetime
from typing import Any, Dict, Optional

from app.core.config import settings
from app.core.timezone import beijing_datetime, to_beijing_datetime
from app.models.task_record import UserTaskRecord


class TaskExecutionDeferred(Exception):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("task execution lease is still active")
        self.retry_after_seconds = max(1, retry_after_seconds)


def prepare_task_execution(task_record: UserTaskRecord) -> bool:
    if task_record.status == "pending":
        _set_execution_lease(task_record)
        return True
    if task_record.status != "running" or getattr(
        task_record, "provider_task_id", None
    ) or _provider_task_id(task_record.extra or {}):
        return False

    retry_after = _active_lease_remaining_seconds(task_record.extra or {})
    if retry_after is not None:
        raise TaskExecutionDeferred(retry_after)
    _set_execution_lease(task_record)
    return True


def _set_execution_lease(task_record: UserTaskRecord) -> None:
    now = beijing_datetime()
    lease_seconds = max(
        60,
        settings.effective_celery_task_time_limit_seconds
        + settings.celery_task_timeout_grace_seconds,
    )
    extra = task_record.extra or {}
    task_record.extra = {
        **extra,
        "execution_attempt": int(extra.get("execution_attempt") or 0) + 1,
        "execution_claimed_at": now.isoformat(),
        "execution_lease_until": datetime.fromtimestamp(
            now.timestamp() + lease_seconds,
            tz=now.tzinfo,
        ).isoformat(),
    }


def _active_lease_remaining_seconds(extra: Dict[str, Any]) -> Optional[int]:
    raw_value = extra.get("execution_lease_until")
    if not raw_value:
        return None
    try:
        lease_until = to_beijing_datetime(datetime.fromisoformat(str(raw_value)))
    except ValueError:
        return None
    remaining = int((lease_until - beijing_datetime()).total_seconds())
    return remaining if remaining > 0 else None


def _provider_task_id(extra: Dict[str, Any]) -> Optional[str]:
    candidates = [
        extra.get("task_id"),
        extra.get("provider_task_id"),
        (extra.get("model_result_extra") or {}).get("task_id"),
        (extra.get("assistant_message_extra") or {}).get("task_id"),
    ]
    for value in candidates:
        if value:
            return str(value)
    return None
