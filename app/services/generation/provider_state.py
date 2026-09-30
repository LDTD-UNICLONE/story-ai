"""Normalize provider task identity, status and progress without querying providers."""

from datetime import timedelta
from typing import Any, Dict, Optional

from sqlalchemy import or_

from app.core.timezone import beijing_datetime
from app.models.task_record import UserTaskRecord
from app.services.generation.provider_polling import provider_poll_interval_seconds


def extract_provider_task_id(extra: Dict[str, Any]) -> Optional[str]:
    candidates = [
        extra.get("task_id"),
        extra.get("provider_task_id"),
        (extra.get("model_result_extra") or {}).get("task_id"),
        (extra.get("assistant_message_extra") or {}).get("task_id"),
        (extra.get("last_provider_task_status") or {}).get("task_id"),
        (extra.get("last_provider_task_status") or {}).get("taskId"),
    ]
    for value in candidates:
        if value:
            return str(value)
    return None


def record_provider_task_state(
    record: UserTaskRecord,
    provider_extra: Dict[str, Any],
    provider_vendor: Optional[str] = None,
) -> bool:
    task_id = extract_provider_task_id(provider_extra)
    if not task_id:
        return False

    now = beijing_datetime()
    provider_status = extract_provider_status(provider_extra)
    if not provider_status:
        provider_status = str(getattr(record, "status", "") or "submitted").lower()

    record.provider_task_id = task_id
    if provider_vendor:
        record.provider_vendor = provider_vendor
    if getattr(record, "provider_submitted_at", None) is None:
        record.provider_submitted_at = now
    record.provider_status = provider_status
    progress_percent = extract_provider_progress_percent(provider_extra)
    if _is_provider_success_status(provider_status):
        progress_percent = 100
    if progress_percent is not None:
        record.extra = {
            **(getattr(record, "extra", None) or {}),
            "progress_percent": progress_percent,
        }
    if _is_provider_terminal_status(provider_status):
        record.next_reconcile_at = None
    elif getattr(record, "last_reconcile_at", None) is None:
        record.next_reconcile_at = now
    else:
        record.next_reconcile_at = now + timedelta(
            seconds=provider_poll_interval_seconds(
                record.generation_type if record is not None else None
            )
        )
    return True


def task_record_progress_percent(record: UserTaskRecord) -> Optional[int]:
    generation_type = str(getattr(record, "generation_type", "") or "")
    if generation_type not in {
        "image",
        "video",
        "asset_image_generate",
        "storyboard_image",
        "storyboard_video",
    }:
        return None
    if str(getattr(record, "status", "") or "").lower() == "success":
        return 100

    extra = getattr(record, "extra", None) or {}
    for candidate in (
        extra,
        extra.get("last_provider_task_status"),
        extra.get("model_result_extra"),
        extra.get("assistant_message_extra"),
    ):
        progress_percent = extract_provider_progress_percent(candidate)
        if progress_percent is not None:
            return progress_percent
    return None


def extract_provider_progress_percent(value: Any) -> Optional[int]:
    if not isinstance(value, dict):
        return None

    candidates = [value]
    provider_response = value.get("provider_response")
    if isinstance(provider_response, dict):
        candidates.append(provider_response)
        data = provider_response.get("data")
        if isinstance(data, dict):
            candidates.append(data)
        elif isinstance(data, list) and data and isinstance(data[0], dict):
            candidates.append(data[0])

    for candidate in candidates:
        raw_progress = candidate.get("progress_percent")
        if raw_progress is None:
            raw_progress = candidate.get("progress")
        progress_percent = _normalize_progress_percent(raw_progress)
        if progress_percent is not None:
            return progress_percent
    return None


def _normalize_progress_percent(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        progress_percent = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return None
    if not 0 <= progress_percent <= 100:
        return None
    return progress_percent


def extract_provider_status(extra: Dict[str, Any]) -> str:
    value = extra.get("task_status") or extra.get("platform_task_status")
    return str(value or "").strip().lower()


def _is_provider_terminal_status(status: str) -> bool:
    return status in {
        "success",
        "succeeded",
        "completed",
        "complete",
        "finished",
        "done",
        "failed",
        "failure",
        "fail",
        "error",
        "cancelled",
        "canceled",
    }


def _is_provider_success_status(status: str) -> bool:
    return status in {"success", "succeeded", "completed", "complete", "finished", "done"}


def has_provider_task_id(record: UserTaskRecord) -> bool:
    return bool(
        getattr(record, "provider_task_id", None) or extract_provider_task_id(record.extra or {})
    )


def provider_task_id_sql_condition():
    extra = UserTaskRecord.extra
    return or_(
        extra["task_id"].as_string().is_not(None),
        extra["provider_task_id"].as_string().is_not(None),
        extra["model_result_extra"]["task_id"].as_string().is_not(None),
        extra["assistant_message_extra"]["task_id"].as_string().is_not(None),
        extra["last_provider_task_status"]["task_id"].as_string().is_not(None),
        extra["last_provider_task_status"]["taskId"].as_string().is_not(None),
    )
