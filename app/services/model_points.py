from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from typing import Any, Dict, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException
from app.models.ai_model import AiModel
from app.models.task_record import UserTaskRecord
from app.services.points import change_user_points


def calculate_model_points_cost(
    ai_model: AiModel,
    *,
    base_points: Optional[int] = None,
    use_cache_multiplier: bool = False,
    use_completion_multiplier: bool = False,
) -> int:
    points = Decimal(base_points if base_points is not None else ai_model.points_cost)
    points *= _decimal(ai_model.model_multiplier)
    if use_cache_multiplier:
        points *= _decimal(ai_model.cache_multiplier)
    if use_completion_multiplier:
        points *= _decimal(ai_model.completion_multiplier)
    points *= _decimal(ai_model.platform_multiplier)
    return max(0, int(points.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))


def calculate_submission_points_cost(
    ai_model: AiModel,
    generation_type: str,
    extra: Optional[Dict[str, Any]] = None,
) -> int:
    if generation_type == "text":
        return calculate_text_submission_points_cost(ai_model)
    if generation_type == "image":
        return calculate_image_model_points_cost(ai_model)
    if generation_type == "video":
        return calculate_video_model_points_cost(extra or {})
    return calculate_model_points_cost(ai_model)


def calculate_text_submission_points_cost(ai_model: AiModel) -> int:
    return max(0, int(ai_model.points_cost or 0))


def calculate_image_model_points_cost(ai_model: AiModel) -> int:
    return max(0, int(ai_model.points_cost or 0))


def calculate_video_model_points_cost(extra: Dict[str, Any]) -> int:
    seconds = _normalize_video_duration_seconds(extra)
    unit_points = 15 if _has_video_reference(extra) else 10
    return seconds * unit_points


def calculate_text_model_points_cost(
    ai_model: AiModel,
    response_extra: Dict[str, Any],
) -> Tuple[Optional[int], Dict[str, Any]]:
    usage = _extract_usage(response_extra)
    if not usage:
        return None, {}

    input_tokens = _first_int(
        usage,
        "prompt_tokens",
        "input_tokens",
        "total_input_tokens",
        "promptTokens",
        "inputTokens",
    )
    output_tokens = _first_int(
        usage,
        "completion_tokens",
        "output_tokens",
        "total_output_tokens",
        "completionTokens",
        "outputTokens",
    )
    cached_tokens = _extract_cached_tokens(usage)
    normal_input_tokens = max(input_tokens - cached_tokens, 0)

    model_multiplier = _decimal(ai_model.model_multiplier)
    cache_multiplier = _decimal(ai_model.cache_multiplier)
    completion_multiplier = _decimal(ai_model.completion_multiplier)
    platform_multiplier = _decimal(ai_model.platform_multiplier)

    real_cost = (
        Decimal(normal_input_tokens) * Decimal("2") * model_multiplier / Decimal("1000000")
        + Decimal(cached_tokens) * Decimal("2") * model_multiplier * cache_multiplier / Decimal("1000000")
        + Decimal(output_tokens) * Decimal("5") * completion_multiplier / Decimal("1000000")
    )
    points_cost = int((real_cost * platform_multiplier).quantize(Decimal("1"), rounding=ROUND_CEILING))
    detail = {
        "normal_input_tokens": normal_input_tokens,
        "cached_tokens": cached_tokens,
        "output_tokens": output_tokens,
        "real_cost": str(real_cost),
        "model_multiplier": str(model_multiplier),
        "cache_multiplier": str(cache_multiplier),
        "completion_multiplier": str(completion_multiplier),
        "platform_multiplier": str(platform_multiplier),
        "points_cost": max(0, points_cost),
    }
    return max(0, points_cost), detail


async def settle_text_task_points(
    db: AsyncSession,
    task_record: UserTaskRecord,
    ai_model: AiModel,
    response_extra: Dict[str, Any],
    *,
    remark_prefix: str,
) -> None:
    actual_points, detail = calculate_text_model_points_cost(ai_model, response_extra)
    if actual_points is None:
        return

    charged_points = task_record.points_cost
    delta = actual_points - charged_points
    settlement_extra: Dict[str, Any] = {
        "points_billing": detail,
        "points_charged_before_settlement": charged_points,
        "points_settled_cost": actual_points,
    }

    if delta < 0:
        transaction = await change_user_points(
            db,
            user_id=task_record.user_id,
            amount=abs(delta),
            transaction_type="refund",
            remark=f"{remark_prefix}按实际用量退回积分：{task_record.title}",
            auto_commit=False,
        )
        settlement_extra["points_refund_transaction_id"] = str(transaction.id)
    elif delta > 0:
        try:
            transaction = await change_user_points(
                db,
                user_id=task_record.user_id,
                amount=-delta,
                transaction_type="consume",
                remark=f"{remark_prefix}按实际用量补扣积分：{task_record.title}",
                auto_commit=False,
            )
            settlement_extra["points_supplement_transaction_id"] = str(transaction.id)
        except AppException as exc:
            if exc.code != 40003:
                raise
            settlement_extra["points_settlement_failed"] = "积分余额不足，未完成补扣"
            settlement_extra["points_settlement_delta"] = delta

    task_record.points_cost = actual_points
    task_record.extra = {**(task_record.extra or {}), **settlement_extra}


def _extract_usage(response_extra: Dict[str, Any]) -> Dict[str, Any]:
    candidates = [
        response_extra.get("usage"),
        (response_extra.get("provider_response") or {}).get("usage"),
        (response_extra.get("model_result_extra") or {}).get("usage"),
        ((response_extra.get("model_result_extra") or {}).get("provider_response") or {}).get("usage"),
    ]
    for candidate in candidates:
        if isinstance(candidate, dict):
            return candidate
    return {}


def _normalize_video_duration_seconds(extra: Dict[str, Any]) -> int:
    seconds = _first_int(
        extra,
        "duration",
        "seconds",
        "generation_seconds",
        "duration_seconds",
        "video_duration_seconds",
    )
    if seconds <= 0:
        seconds = 5
    return min(max(seconds, 5), 15)


def _has_video_reference(extra: Dict[str, Any]) -> bool:
    for key in ("video", "video_url", "video_urls", "videos", "reference_video", "reference_videos"):
        if _has_value(extra.get(key)):
            return True
    for key in ("media", "media_items", "content"):
        for item in _as_list(extra.get(key)):
            if _looks_like_video_media(item):
                return True
    return False


def _looks_like_video_media(value: Any) -> bool:
    if isinstance(value, dict):
        media_type = str(value.get("type") or "").lower()
        role = str(value.get("role") or "").lower()
        return (
            media_type == "video_url"
            or "video" in role
            or _has_value(value.get("video_url"))
            or _has_value(value.get("video"))
        )
    if isinstance(value, str):
        return value.lower().split("?", 1)[0].endswith((".mp4", ".mov", ".webm", ".m4v", ".avi", ".mkv"))
    return False


def _has_value(value: Any) -> bool:
    if value in (None, "", []):
        return False
    if isinstance(value, dict):
        return any(_has_value(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_value(item) for item in value)
    return True


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _extract_cached_tokens(usage: Dict[str, Any]) -> int:
    direct = _first_int(
        usage,
        "cached_tokens",
        "cache_tokens",
        "cachedTokens",
        "cache_read_input_tokens",
        "cacheReadInputTokens",
    )
    nested_sources = [
        usage.get("prompt_tokens_details"),
        usage.get("input_tokens_details"),
        usage.get("promptTokensDetails"),
        usage.get("inputTokensDetails"),
    ]
    nested = 0
    for source in nested_sources:
        if isinstance(source, dict):
            nested = max(
                nested,
                _first_int(
                    source,
                    "cached_tokens",
                    "cache_tokens",
                    "cachedTokens",
                    "cache_read",
                    "cacheRead",
                ),
            )
    return max(direct, nested)


def _first_int(data: Dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = data.get(key)
        if value in (None, ""):
            continue
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            digits = "".join(char for char in str(value) if char.isdigit())
            if digits:
                return max(0, int(digits))
    return 0


def _decimal(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value or "1"))
