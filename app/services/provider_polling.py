from typing import Any, Dict, Optional

from app.core.config import settings


def provider_poll_interval_seconds(generation_type: Optional[str]) -> int:
    normalized = str(generation_type or "").strip()
    if normalized in {"video", "storyboard_video"}:
        return max(10, settings.provider_task_video_poll_interval_seconds)
    if normalized in {"image", "asset_image_generate"}:
        return max(5, settings.provider_task_image_poll_interval_seconds)
    return max(5, settings.provider_task_poll_interval_seconds)


def provider_next_poll_seconds(
    generation_type: Optional[str],
    status: str,
    extra: Optional[Dict[str, Any]] = None,
) -> Optional[int]:
    if status not in {"pending", "running"}:
        return None
    extra = extra or {}
    next_poll_seconds = extra.get("next_poll_seconds")
    if isinstance(next_poll_seconds, int) and next_poll_seconds > 0:
        return next_poll_seconds
    return provider_poll_interval_seconds(generation_type)
