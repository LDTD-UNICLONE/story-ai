"""One supplier query cadence; platform status reads have their own short hint."""

from typing import Any, Dict, Optional

from app.core.config import settings
from app.services.generation.runner import ModelRunResult


def provider_poll_interval_seconds(generation_type: Optional[str] = None) -> int:
    return max(1, settings.provider_task_poll_interval_seconds)


def provider_next_poll_seconds(
    generation_type: Optional[str],
    status: str,
    extra: Optional[Dict[str, Any]] = None,
) -> Optional[int]:
    if status not in {"pending", "running"}:
        return None
    # These requests only read platform state. Historical supplier hints must
    # not postpone displaying a result that has already been persisted.
    return 1


def defer_provider_task_result(model_result: ModelRunResult, task_id: str) -> ModelRunResult:
    """Release submission workers; the common reconciler owns accepted jobs."""
    model_result.extra = {
        **model_result.extra,
        "platform_task_status": "running",
        "provider_polling_deferred": True,
        "next_poll_seconds": provider_next_poll_seconds(None, "running"),
    }
    model_result.content = f"模型任务仍在生成中：{task_id}"
    return model_result
