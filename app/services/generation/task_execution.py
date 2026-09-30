from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.timezone import beijing_datetime, to_beijing_datetime
from app.models.task_record import UserTaskRecord
from app.services.generation.provider_state import has_provider_task_id


async def lock_active_task(
    db: AsyncSession,
    task_record: UserTaskRecord,
    *,
    allow_provider_task: bool = True,
) -> bool:
    """Refresh before writing; keep the task lock until the caller commits or rolls back.

    Do not flush stale ORM state before checking the persisted terminal status.
    Provider-owned tasks can finish here, but failures/retries must go through recovery.
    """
    with db.no_autoflush:
        await db.refresh(task_record, with_for_update=True)
    return (
        task_record.status in {"pending", "running"}
        and not (task_record.extra or {}).get("interrupted")
        and (allow_provider_task or not has_provider_task_id(task_record))
    )


class TaskExecutionDeferred(Exception):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("task execution lease is still active")
        self.retry_after_seconds = max(1, retry_after_seconds)


def prepare_task_execution(task_record: UserTaskRecord) -> bool:
    if has_provider_task_id(task_record):
        return False
    if task_record.status == "pending":
        _set_execution_lease(task_record)
        return True
    if task_record.status != "running":
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
