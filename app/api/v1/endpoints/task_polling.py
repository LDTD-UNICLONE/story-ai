from typing import Optional

from fastapi import Response

from app.models.task_record import UserTaskRecord
from app.services.generation.provider_polling import provider_next_poll_seconds


def task_next_poll_seconds(task_record: UserTaskRecord) -> Optional[int]:
    return provider_next_poll_seconds(
        task_record.generation_type,
        task_record.status,
        task_record.extra or {},
    )


def set_task_poll_headers(response: Response, next_poll_seconds: Optional[int]) -> None:
    response.headers["Cache-Control"] = "no-store"
    if next_poll_seconds:
        response.headers["X-Next-Poll-Seconds"] = str(next_poll_seconds)
