import re
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from types import SimpleNamespace
from typing import Any, Dict, Optional, Sequence, Tuple
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException
from app.integrations.apimart import APIMART_VENDOR
from app.integrations.apimart_video_specs import (
    is_apimart_video_model,
    video_billing_duration_seconds,
)
from app.integrations.volcengine_ark_video_specs import (
    is_volcengine_ark_video_model,
    merge_video_capabilities as merge_ark_video_capabilities,
    normalize_video_resolution,
)
from app.models.ai_model import AiModel
from app.models.task_record import UserTaskRecord
from app.services.billing.policy import (
    APIMART_TEXT_PRICING_UNIT,
    validate_model_billing_policy,
)
from app.services.models.configuration import (
    build_model_configuration,
    model_billing_value,
    model_request_capabilities,
)
from app.services.billing.points import change_user_points, ensure_user_points_enough


# 4K is only available on the standard Seedance 2.0 model.
SEEDANCE_VIDEO_UNIT_POINTS = {
    "doubao-seedance-2-0-260128": {
        "no_video": {"480p": 6, "720p": 12, "1080p": 30, "4k": 60},
        "with_video": {"480p": 7, "720p": 13, "1080p": 35, "4k": 80},
    },
    "doubao-seedance-2-0-fast-260128": {
        "no_video": {"480p": 5, "720p": 10},
        "with_video": {"480p": 6, "720p": 12},
    },
}

_APIMART_CREDITS_TO_CNY = Decimal("0.7")
_PLATFORM_POINTS_PER_CNY = Decimal("10")
_IMAGE_PIXEL_SIZE_PATTERN = re.compile(r"^([1-9]\d{1,4})[xX×]([1-9]\d{1,4})$")


def build_model_billing_snapshot(ai_model: AiModel) -> Dict[str, Any]:
    return {
        "model_id": ai_model.model_id,
        "vendor": ai_model.vendor,
        "model_type": ai_model.model_type,
        "configuration": build_model_configuration(ai_model),
    }


def calculate_apimart_actual_points_cost(
    ai_model: AiModel,
    response_extra: Dict[str, Any],
    *,
    request_extra: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[int], Dict[str, Any]]:
    if ai_model.vendor != APIMART_VENDOR:
        return None, {}

    credits_cost = _find_provider_decimal(response_extra, "credits_cost")
    provider_cost = _find_provider_decimal(response_extra, "cost")
    if credits_cost is None and provider_cost is None:
        return None, {}

    billing_source = "provider_credits_cost"
    if credits_cost is None:
        credits_cost = provider_cost * Decimal("10")
        billing_source = "provider_cost"
    if provider_cost is None:
        provider_cost = credits_cost / Decimal("10")

    provider_cost_cny = credits_cost * _APIMART_CREDITS_TO_CNY
    provider_cost_points = _ceil_points(provider_cost_cny * _PLATFORM_POINTS_PER_CNY)
    platform_rate = _decimal(model_billing_value(ai_model, "platform"))
    if ai_model.model_type == "image":
        image_policy = _model_billing_policy(ai_model, "image")
        pricing_mode = "admin_image_policy" if image_policy else "fixed_admin_points"
        user_points_cost = calculate_image_model_points_cost(
            ai_model,
            request_extra or {},
        )
    else:
        pricing_mode = "actual_cost_platform_rate"
        user_points_cost = _ceil_points(Decimal(provider_cost_points) * platform_rate)
    user_charge_cny = Decimal(user_points_cost) / _PLATFORM_POINTS_PER_CNY
    gross_profit_cny = user_charge_cny - provider_cost_cny
    gross_margin = gross_profit_cny / user_charge_cny if user_charge_cny > 0 else Decimal("0")
    expected_credits_cost = provider_cost * Decimal("10")
    detail = {
        "billing_type": "apimart_actual_cost",
        "billing_source": billing_source,
        "provider_cost_usd": str(provider_cost),
        "provider_credits_cost": str(credits_cost),
        "provider_cost_cny": str(provider_cost_cny),
        "provider_cost_points": provider_cost_points,
        "pricing_mode": pricing_mode,
        "platform_rate": str(platform_rate),
        "user_points_cost": user_points_cost,
        "user_charge_cny": str(user_charge_cny),
        "gross_profit_cny": str(gross_profit_cny),
        "gross_margin": str(gross_margin),
        "credits_to_cny_rate": str(_APIMART_CREDITS_TO_CNY),
        "points_per_cny": str(_PLATFORM_POINTS_PER_CNY),
    }
    if credits_cost != expected_credits_cost:
        detail["provider_cost_discrepancy"] = str(credits_cost - expected_credits_cost)
    return user_points_cost, detail


def calculate_model_points_cost(
    ai_model: AiModel,
    *,
    base_points: Optional[int] = None,
    use_cache_multiplier: bool = False,
    use_completion_multiplier: bool = False,
) -> int:
    points = Decimal(
        base_points if base_points is not None else model_billing_value(ai_model, "base_points")
    )
    points *= _decimal(model_billing_value(ai_model, "model"))
    if use_cache_multiplier:
        points *= _decimal(model_billing_value(ai_model, "cache"))
    if use_completion_multiplier:
        points *= _decimal(model_billing_value(ai_model, "completion"))
    points *= _decimal(model_billing_value(ai_model, "platform"))
    return max(0, int(points.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))


def calculate_submission_points_cost(
    ai_model: AiModel,
    generation_type: str,
    extra: Optional[Dict[str, Any]] = None,
) -> int:
    if generation_type == "text":
        return calculate_text_submission_points_cost(ai_model)
    if generation_type == "image":
        return calculate_image_model_points_cost(ai_model, extra or {})
    if generation_type == "video":
        return calculate_video_model_points_cost(ai_model, extra or {})
    return calculate_model_points_cost(ai_model)


def calculate_text_submission_points_cost(ai_model: AiModel) -> int:
    # Text tasks are settled from provider cost or token usage after completion.
    # base_points is an account-balance gate, not a submission charge.
    return 0


def model_minimum_balance_points(ai_model: AiModel) -> int:
    if ai_model.model_type not in {"text", "video"}:
        return 0
    return max(0, int(model_billing_value(ai_model, "base_points") or 0))


async def ensure_model_minimum_balance(
    db: AsyncSession,
    user_id: UUID,
    ai_model: AiModel,
) -> None:
    await ensure_user_points_enough(
        db,
        user_id,
        model_minimum_balance_points(ai_model),
    )


def calculate_image_model_points_cost(
    ai_model: AiModel,
    extra: Optional[Dict[str, Any]] = None,
) -> int:
    policy = _model_billing_policy(ai_model, "image")
    if not policy:
        return max(0, int(model_billing_value(ai_model, "base_points") or 0))

    billing_extra = extra or {}
    quantity = _positive_first_int(
        billing_extra,
        "n",
        "count",
        "num_images",
        "number_of_images",
        default=1,
    )
    resolution_multipliers = {
        str(key).strip().lower(): _decimal(value)
        for key, value in (policy.get("resolution_multipliers") or {}).items()
    }
    requested_resolution = _image_billing_resolution(policy, billing_extra)
    default_resolution = _normalized_text(policy.get("default_resolution"))
    resolution = requested_resolution or default_resolution
    resolution_multiplier = resolution_multipliers.get(resolution)
    if resolution_multiplier is None:
        resolution_multiplier = resolution_multipliers.get(
            "default",
            resolution_multipliers.get(default_resolution, Decimal("1")),
        )

    reference_count = _image_reference_count(billing_extra)
    free_reference_images = max(0, int(policy.get("free_reference_images", 0)))
    paid_reference_images = max(reference_count - free_reference_images, 0)
    additional_reference_points = _decimal(policy.get("points_per_additional_reference_image", "0"))

    points = (
        Decimal(max(0, int(model_billing_value(ai_model, "base_points") or 0)))
        * Decimal(quantity)
        * resolution_multiplier
        + Decimal(paid_reference_images) * additional_reference_points
    ) * (
        _decimal(model_billing_value(ai_model, "model"))
        * _decimal(model_billing_value(ai_model, "platform"))
    )
    return _ceil_points(points)


def calculate_video_model_points_cost(ai_model: AiModel, extra: Dict[str, Any]) -> int:
    seconds = _normalize_video_duration_seconds(ai_model, extra)
    policy = _model_billing_policy(ai_model, "video")
    if not policy:
        points = Decimal(seconds * _video_unit_points(ai_model, extra))
        if ai_model.vendor == APIMART_VENDOR:
            points *= _decimal(model_billing_value(ai_model, "platform"))
        return _ceil_points(points)

    unit_points, _, _ = _video_policy_rate(policy, extra)
    points = (
        Decimal(seconds)
        * unit_points
        * _decimal(model_billing_value(ai_model, "model"))
        * _decimal(model_billing_value(ai_model, "platform"))
    )
    return _ceil_points(points)


def summarize_base_points_recommendation(
    samples: Sequence[int],
    *,
    current_base_points: int,
) -> Dict[str, Any]:
    """Summarize actual charges for an admin-reviewed base-points recommendation."""
    values = sorted(max(0, int(value)) for value in samples)
    sample_count = len(values)
    if sample_count == 0:
        return {
            "sample_count": 0,
            "confidence": "none",
            "safe_to_apply": False,
            "minimum_points": None,
            "p50_points": None,
            "p90_points": None,
            "p95_points": None,
            "maximum_points": None,
            "recommended_base_points": None,
            "recommended_precharge_points": None,
            "current_base_points": max(0, int(current_base_points)),
        }

    confidence = "high" if sample_count >= 20 else "medium" if sample_count >= 5 else "low"
    p50_points = _nearest_rank_percentile(values, Decimal("0.50"))
    p90_points = _nearest_rank_percentile(values, Decimal("0.90"))
    p95_points = _nearest_rank_percentile(values, Decimal("0.95"))
    return {
        "sample_count": sample_count,
        "confidence": confidence,
        "safe_to_apply": sample_count >= 5,
        "minimum_points": values[0],
        "p50_points": p50_points,
        "p90_points": p90_points,
        "p95_points": p95_points,
        "maximum_points": values[-1],
        "recommended_base_points": p95_points,
        "recommended_precharge_points": p95_points,
        "current_base_points": max(0, int(current_base_points)),
    }


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
    policy = _model_billing_policy(ai_model, "text")
    if _is_apimart_text_credits_policy(ai_model, policy):
        return _calculate_apimart_text_usage_points(
            ai_model,
            usage,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            policy=policy,
        )

    cached_tokens = _extract_cached_tokens(usage)
    normal_input_tokens = max(input_tokens - cached_tokens, 0)
    input_rate = _decimal(policy.get("input_points_per_million", "2"))
    cached_input_rate = _decimal(policy.get("cached_input_points_per_million", "2"))
    output_rate = _decimal(policy.get("output_points_per_million", "5"))
    minimum_points = max(0, int(policy.get("minimum_points", 0))) if policy else 0

    model_multiplier = _decimal(model_billing_value(ai_model, "model"))
    cache_multiplier = _decimal(model_billing_value(ai_model, "cache"))
    completion_multiplier = _decimal(model_billing_value(ai_model, "completion"))
    platform_multiplier = _decimal(model_billing_value(ai_model, "platform"))

    real_cost = (
        Decimal(normal_input_tokens) * input_rate * model_multiplier / Decimal("1000000")
        + Decimal(cached_tokens)
        * cached_input_rate
        * model_multiplier
        * cache_multiplier
        / Decimal("1000000")
        + Decimal(output_tokens) * output_rate * completion_multiplier / Decimal("1000000")
    )
    points_cost = max(minimum_points, _ceil_points(real_cost * platform_multiplier))
    detail = {
        "billing_policy_type": "text" if policy else "default_text",
        "normal_input_tokens": normal_input_tokens,
        "cached_tokens": cached_tokens,
        "output_tokens": output_tokens,
        "input_points_per_million": str(input_rate),
        "cached_input_points_per_million": str(cached_input_rate),
        "output_points_per_million": str(output_rate),
        "minimum_points": minimum_points,
        "real_cost": str(real_cost),
        "model_multiplier": str(model_multiplier),
        "cache_multiplier": str(cache_multiplier),
        "completion_multiplier": str(completion_multiplier),
        "platform_multiplier": str(platform_multiplier),
        "points_cost": points_cost,
    }
    return points_cost, detail


async def settle_text_task_points(
    db: AsyncSession,
    task_record: UserTaskRecord,
    ai_model: AiModel,
    response_extra: Dict[str, Any],
    *,
    remark_prefix: str,
) -> None:
    if (task_record.extra or {}).get("points_settled"):
        return

    billing_model = _task_billing_model(task_record, ai_model)
    actual_points, detail = calculate_apimart_actual_points_cost(billing_model, response_extra)
    if actual_points is None:
        actual_points, detail = calculate_text_model_points_cost(billing_model, response_extra)
    if actual_points is None:
        _mark_precharge_as_final(task_record, billing_type="text_precharge_fallback")
        return

    charged_points = task_record.points_cost
    delta = actual_points - charged_points
    settlement_extra: Dict[str, Any] = {
        **_billing_details_for_settlement(detail),
        "points_charged_before_settlement": charged_points,
        "points_settled_cost": actual_points,
    }
    await _apply_points_settlement(
        db,
        task_record,
        actual_points=actual_points,
        delta=delta,
        settlement_extra=settlement_extra,
        remark_prefix=remark_prefix,
        settlement_basis="按实际用量",
    )


async def settle_image_task_points(
    db: AsyncSession,
    task_record: UserTaskRecord,
    ai_model: AiModel,
    response_extra: Dict[str, Any],
    *,
    remark_prefix: str,
) -> None:
    if (task_record.extra or {}).get("points_settled"):
        return

    billing_model = _task_billing_model(task_record, ai_model)
    actual_points, detail = calculate_apimart_actual_points_cost(
        billing_model,
        response_extra,
        request_extra=_task_request_extra(task_record),
    )
    if actual_points is None:
        billing_type = (
            "image_policy" if _model_billing_policy(billing_model, "image") else "fixed_image"
        )
        _mark_precharge_as_final(task_record, billing_type=billing_type)
        return

    charged_points = task_record.points_cost
    delta = actual_points - charged_points
    settlement_extra: Dict[str, Any] = {
        **_billing_details_for_settlement(detail),
        "points_charged_before_settlement": charged_points,
        "points_settled_cost": actual_points,
    }
    await _apply_points_settlement(
        db,
        task_record,
        actual_points=actual_points,
        delta=delta,
        settlement_extra=settlement_extra,
        remark_prefix=remark_prefix,
        settlement_basis="按图片计费策略",
    )


async def settle_video_task_points(
    db: AsyncSession,
    task_record: UserTaskRecord,
    ai_model: AiModel,
    request_extra: Optional[Dict[str, Any]] = None,
    *,
    remark_prefix: str,
) -> None:
    if (task_record.extra or {}).get("points_settled"):
        return

    billing_extra = request_extra or _task_request_extra(task_record)
    billing_model = _task_billing_model(task_record, ai_model)
    actual_points, provider_cost_detail = calculate_apimart_actual_points_cost(
        billing_model, task_record.extra or {}
    )
    if actual_points is None:
        actual_points = calculate_video_model_points_cost(billing_model, billing_extra)
    charged_points = task_record.points_cost
    delta = actual_points - charged_points
    duration_seconds = _normalize_video_duration_seconds(billing_model, billing_extra)
    policy = _model_billing_policy(billing_model, "video")
    if provider_cost_detail:
        points_billing = provider_cost_detail
    elif policy:
        unit_points, resolution, reference_type = _video_policy_rate(policy, billing_extra)
        points_billing = {
            "billing_type": "video",
            "duration_seconds": duration_seconds,
            "reference_type": reference_type,
            "has_video_reference": reference_type in {"video", "multimodal"},
            "resolution": resolution,
            "unit_points": str(unit_points),
            "model_multiplier": str(_decimal(model_billing_value(billing_model, "model"))),
            "platform_multiplier": str(_decimal(model_billing_value(billing_model, "platform"))),
            "points_cost": actual_points,
        }
    else:
        unit_points = Decimal(_video_unit_points(billing_model, billing_extra))
        resolution = _video_billing_resolution(billing_model, billing_extra)
        reference_type = "video" if _has_video_reference(billing_extra) else "none"
        points_billing = {
            "billing_type": "default_video",
            "duration_seconds": duration_seconds,
            "reference_type": reference_type,
            "has_video_reference": reference_type in {"video", "multimodal"},
            "resolution": resolution,
            "unit_points": str(unit_points),
            "model_multiplier": str(_decimal(model_billing_value(billing_model, "model"))),
            "platform_multiplier": str(_decimal(model_billing_value(billing_model, "platform"))),
            "points_cost": actual_points,
        }
    settlement_extra: Dict[str, Any] = {
        **_billing_details_for_settlement(points_billing),
        "points_charged_before_settlement": charged_points,
        "points_settled_cost": actual_points,
    }
    await _apply_points_settlement(
        db,
        task_record,
        actual_points=actual_points,
        delta=delta,
        settlement_extra=settlement_extra,
        remark_prefix=remark_prefix,
        settlement_basis="按实际视频参数",
    )


async def refund_task_points(
    db: AsyncSession,
    record: UserTaskRecord,
    *,
    remark_prefix: str,
) -> None:
    """Refund once within the caller's transaction; the caller holds the task row lock."""
    if record.points_cost <= 0 or (record.extra or {}).get("refund_transaction_id"):
        return
    refund_transaction = await change_user_points(
        db,
        user_id=record.user_id,
        amount=record.points_cost,
        transaction_type="refund",
        remark=f"{remark_prefix}：{record.title}",
        auto_commit=False,
    )
    record.extra = {
        **(record.extra or {}),
        "refund_transaction_id": str(refund_transaction.id),
    }


async def _apply_points_settlement(
    db: AsyncSession,
    task_record: UserTaskRecord,
    *,
    actual_points: int,
    delta: int,
    settlement_extra: Dict[str, Any],
    remark_prefix: str,
    settlement_basis: str,
) -> None:
    if delta < 0:
        transaction = await change_user_points(
            db,
            user_id=task_record.user_id,
            amount=abs(delta),
            transaction_type="refund",
            remark=f"{remark_prefix}{settlement_basis}退回积分：{task_record.title}",
            auto_commit=False,
        )
        settlement_extra["points_refund_transaction_id"] = str(transaction.id)
    elif delta > 0:
        transaction = await change_user_points(
            db,
            user_id=task_record.user_id,
            amount=-delta,
            transaction_type="consume",
            remark=f"{remark_prefix}{settlement_basis}补扣积分：{task_record.title}",
            auto_commit=False,
            allow_negative_balance=True,
        )
        settlement_extra["points_supplement_transaction_id"] = str(transaction.id)

    task_record.points_cost = actual_points
    task_record.extra = {
        **(task_record.extra or {}),
        **settlement_extra,
        "points_settled": True,
    }


def _mark_precharge_as_final(
    task_record: UserTaskRecord,
    *,
    billing_type: str,
) -> None:
    charged_points = max(0, int(task_record.points_cost or 0))
    task_record.extra = {
        **(task_record.extra or {}),
        "points_billing": {
            "billing_type": billing_type,
            "points_cost": charged_points,
        },
        "points_charged_before_settlement": charged_points,
        "points_settled_cost": charged_points,
        "points_settled": True,
    }


def _task_request_extra(task_record: UserTaskRecord) -> Dict[str, Any]:
    extra = task_record.extra or {}
    for key in ("user_message_extra", "model_extra"):
        value = extra.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _image_billing_resolution(
    policy: Dict[str, Any],
    extra: Dict[str, Any],
) -> str:
    size = _normalized_text(extra.get("size"))
    pixel_match = _IMAGE_PIXEL_SIZE_PATTERN.fullmatch(size)
    if pixel_match:
        total_pixels = int(pixel_match.group(1)) * int(pixel_match.group(2))
        tiers = sorted(
            (
                (int(max_pixels), _normalized_text(resolution))
                for resolution, max_pixels in (policy.get("pixel_tiers") or {}).items()
            ),
            key=lambda item: item[0],
        )
        for max_pixels, resolution in tiers:
            if total_pixels <= max_pixels:
                return resolution
        if tiers:
            return tiers[-1][1]

    known_resolutions = {
        _normalized_text(value)
        for value in (
            *(policy.get("resolution_multipliers") or {}).keys(),
            *(policy.get("pixel_tiers") or {}).keys(),
        )
    }
    if size in known_resolutions:
        return size
    return _normalized_text(extra.get("resolution") or extra.get("quality"))


def _image_reference_count(extra: Dict[str, Any]) -> int:
    for key in (
        "image_urls",
        "reference_images",
        "images",
        "uploaded_images",
        "image_url",
        "reference_image",
    ):
        if key not in extra:
            continue
        return sum(1 for item in _as_list(extra.get(key)) if _has_value(item))
    return 0


def _billing_details_for_settlement(detail: Dict[str, Any]) -> Dict[str, Any]:
    if detail.get("billing_type") not in {
        "apimart_actual_cost",
        "apimart_usage_credits",
    }:
        return {"points_billing": detail}
    return {
        "points_billing": {
            key: detail[key]
            for key in (
                "billing_type",
                "billing_source",
                "user_points_cost",
                "user_charge_cny",
            )
        },
        "provider_cost_billing": detail,
    }


def _task_billing_model(task_record: UserTaskRecord, ai_model: AiModel) -> AiModel:
    snapshot = (task_record.extra or {}).get("model_billing_snapshot")
    if not isinstance(snapshot, dict) or not snapshot:
        return ai_model
    current = build_model_billing_snapshot(ai_model)
    return SimpleNamespace(**{key: snapshot.get(key, value) for key, value in current.items()})


def _extract_usage(response_extra: Dict[str, Any]) -> Dict[str, Any]:
    candidates = [
        response_extra.get("usage"),
        (response_extra.get("provider_response") or {}).get("usage"),
        (response_extra.get("model_result_extra") or {}).get("usage"),
        ((response_extra.get("model_result_extra") or {}).get("provider_response") or {}).get(
            "usage"
        ),
    ]
    for candidate in candidates:
        if isinstance(candidate, dict):
            return candidate
    return {}


def _find_provider_decimal(value: Any, field: str) -> Optional[Decimal]:
    queue = [value]
    while queue:
        current = queue.pop(0)
        if isinstance(current, dict):
            parsed = _optional_non_negative_decimal(current.get(field))
            if parsed is not None:
                return parsed
            for key in (
                "provider_response",
                "model_result_extra",
                "assistant_message_extra",
                "data",
            ):
                nested = current.get(key)
                if isinstance(nested, (dict, list)):
                    queue.append(nested)
        elif isinstance(current, list):
            queue.extend(item for item in current if isinstance(item, (dict, list)))
    return None


def _optional_non_negative_decimal(value: Any) -> Optional[Decimal]:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except Exception:
        return None
    if not parsed.is_finite() or parsed < 0:
        return None
    return parsed


def _normalize_video_duration_seconds(ai_model: AiModel, extra: Dict[str, Any]) -> int:
    duration_keys = (
        "duration",
        "seconds",
        "generation_seconds",
        "duration_seconds",
        "video_duration_seconds",
    )
    policy = _model_billing_policy(ai_model, "video")
    raw_duration = next(
        (extra.get(key) for key in duration_keys if extra.get(key) not in (None, "")),
        None,
    )
    if policy:
        if raw_duration in (None, ""):
            return int(policy.get("default_duration_seconds", 5))
        if ai_model.vendor == APIMART_VENDOR and is_apimart_video_model(ai_model.model_id):
            return video_billing_duration_seconds(ai_model.model_id, raw_duration)
        try:
            seconds = int(raw_duration)
        except (TypeError, ValueError):
            seconds = int(policy.get("default_duration_seconds", 5))
        return max(1, seconds)
    if ai_model.vendor == APIMART_VENDOR:
        return video_billing_duration_seconds(ai_model.model_id, raw_duration)
    seconds = _first_int(extra, *duration_keys)
    if seconds <= 0:
        seconds = 5
    return min(max(seconds, 5), 15)


def _video_unit_points(ai_model: AiModel, extra: Dict[str, Any]) -> int:
    seedance_model_id = _seedance_billing_model_id(ai_model.model_id)
    has_video_reference = _has_video_reference(extra)
    if seedance_model_id:
        group = "with_video" if has_video_reference else "no_video"
        resolution = _normalize_seedance_billing_resolution(ai_model, extra)
        return SEEDANCE_VIDEO_UNIT_POINTS[seedance_model_id][group][resolution]
    return 15 if has_video_reference else 10


def _seedance_billing_model_id(model_id: str) -> Optional[str]:
    normalized = model_id.strip().lower()
    for supported_model_id in SEEDANCE_VIDEO_UNIT_POINTS:
        if (
            normalized == supported_model_id
            or normalized.endswith(supported_model_id)
            or supported_model_id in normalized
        ):
            return supported_model_id
    if is_volcengine_ark_video_model(normalized):
        return None
    return None


def _normalize_seedance_billing_resolution(ai_model: AiModel, extra: Dict[str, Any]) -> str:
    capabilities = merge_ark_video_capabilities(
        ai_model.model_id,
        model_request_capabilities(ai_model),
    )
    return normalize_video_resolution(extra.get("resolution"), capabilities)


def _video_billing_resolution(ai_model: AiModel, extra: Dict[str, Any]) -> Optional[str]:
    if _seedance_billing_model_id(ai_model.model_id):
        return _normalize_seedance_billing_resolution(ai_model, extra)
    raw_resolution = extra.get("resolution")
    return str(raw_resolution) if raw_resolution not in (None, "") else None


def _video_policy_rate(
    policy: Dict[str, Any],
    extra: Dict[str, Any],
) -> tuple[Decimal, str, str]:
    rates = {
        str(resolution).strip().lower(): {
            str(reference_type).strip().lower(): _decimal(value)
            for reference_type, value in reference_rates.items()
        }
        for resolution, reference_rates in policy["rates"].items()
    }
    requested_resolution = _normalized_text(extra.get("resolution"))
    default_resolution = _normalized_text(policy.get("default_resolution"))
    resolution = requested_resolution or default_resolution
    if resolution not in rates:
        resolution = "default" if "default" in rates else default_resolution
    if not resolution or resolution not in rates:
        raise AppException("请求分辨率没有对应的视频计费费率", code=40062, status_code=400)

    reference_type, present_reference_types = _video_reference_type(extra)
    reference_rates = rates[resolution]
    selected_reference_type = reference_type
    if selected_reference_type not in reference_rates and reference_type == "multimodal":
        selected_reference_type = next(
            (
                candidate
                for candidate in ("video", "audio", "image")
                if candidate in present_reference_types and candidate in reference_rates
            ),
            selected_reference_type,
        )
    if selected_reference_type not in reference_rates and "none" in reference_rates:
        selected_reference_type = "none"
    if selected_reference_type not in reference_rates:
        raise AppException(
            f"参考类型 {reference_type} 没有对应的视频计费费率",
            code=40062,
            status_code=400,
        )
    return reference_rates[selected_reference_type], resolution, reference_type


def _video_reference_type(extra: Dict[str, Any]) -> tuple[str, set[str]]:
    present: set[str] = set()
    if _has_video_reference(extra):
        present.add("video")
    if _has_image_reference(extra):
        present.add("image")
    if _has_audio_reference(extra):
        present.add("audio")
    if len(present) > 1:
        return "multimodal", present
    if present:
        return next(iter(present)), present
    return "none", present


def _has_video_reference(extra: Dict[str, Any]) -> bool:
    for key in (
        "video",
        "video_url",
        "video_urls",
        "videos",
        "uploaded_videos",
        "reference_video",
        "reference_video_url",
        "reference_video_urls",
        "reference_videos",
        "extend_from_task_id",
    ):
        if _has_value(extra.get(key)):
            return True
    for key in ("media", "media_items", "content"):
        for item in _as_list(extra.get(key)):
            if _looks_like_video_media(item):
                return True
    return False


def _has_image_reference(extra: Dict[str, Any]) -> bool:
    for key in (
        "image",
        "image_url",
        "image_urls",
        "images",
        "uploaded_images",
        "reference_image",
        "reference_image_url",
        "reference_image_urls",
        "reference_images",
        "image_with_roles",
        "img_references",
        "first_frame_url",
        "first_frame",
        "first_frame_image",
        "first_image_url",
        "last_frame_url",
        "last_frame",
        "last_frame_image",
        "last_image_url",
        "avatar_id",
    ):
        if _has_value(extra.get(key)):
            return True
    for key in ("media", "media_items", "content"):
        for item in _as_list(extra.get(key)):
            if _looks_like_image_media(item):
                return True
    return False


def _has_audio_reference(extra: Dict[str, Any]) -> bool:
    for key in (
        "audio",
        "audio_url",
        "audio_urls",
        "audios",
        "uploaded_audios",
        "reference_audio",
        "reference_audio_url",
        "reference_audio_urls",
        "reference_audios",
    ):
        if _has_value(extra.get(key)):
            return True
    for key in ("media", "media_items", "content"):
        for item in _as_list(extra.get(key)):
            if _looks_like_audio_media(item):
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
        return (
            value.lower()
            .split("?", 1)[0]
            .endswith((".mp4", ".mov", ".webm", ".m4v", ".avi", ".mkv"))
        )
    return False


def _looks_like_image_media(value: Any) -> bool:
    if isinstance(value, dict):
        media_type = str(value.get("type") or "").lower()
        role = str(value.get("role") or "").lower()
        return (
            "image" in media_type
            or "image" in role
            or _has_value(value.get("image_url"))
            or _has_value(value.get("image"))
        )
    if isinstance(value, str):
        return (
            value.lower()
            .split("?", 1)[0]
            .endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif"))
        )
    return False


def _looks_like_audio_media(value: Any) -> bool:
    if isinstance(value, dict):
        media_type = str(value.get("type") or "").lower()
        role = str(value.get("role") or "").lower()
        return (
            "audio" in media_type
            or "audio" in role
            or _has_value(value.get("audio_url"))
            or _has_value(value.get("audio"))
        )
    if isinstance(value, str):
        return (
            value.lower()
            .split("?", 1)[0]
            .endswith((".mp3", ".wav", ".aac", ".m4a", ".flac", ".ogg"))
        )
    return False


def _has_value(value: Any) -> bool:
    if value is False or value in (None, "", []):
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


def _extract_cache_write_tokens(usage: Dict[str, Any]) -> int:
    direct = _first_int(
        usage,
        "cache_write_tokens",
        "cache_creation_input_tokens",
        "cacheWriteTokens",
    )
    nested = 0
    for source in (
        usage.get("prompt_tokens_details"),
        usage.get("input_tokens_details"),
        usage.get("promptTokensDetails"),
        usage.get("inputTokensDetails"),
    ):
        if isinstance(source, dict):
            nested = max(
                nested,
                _first_int(
                    source,
                    "cache_write_tokens",
                    "cache_creation_input_tokens",
                    "cacheWriteTokens",
                ),
            )
    return max(direct, nested)


def _extract_reasoning_tokens(usage: Dict[str, Any]) -> int:
    direct = _first_int(usage, "reasoning_tokens", "reasoningTokens")
    nested = 0
    for source in (
        usage.get("completion_tokens_details"),
        usage.get("output_tokens_details"),
        usage.get("completionTokensDetails"),
        usage.get("outputTokensDetails"),
    ):
        if isinstance(source, dict):
            nested = max(
                nested,
                _first_int(source, "reasoning_tokens", "reasoningTokens"),
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


def _positive_first_int(
    data: Dict[str, Any],
    *keys: str,
    default: int,
) -> int:
    value = _first_int(data, *keys)
    return value if value > 0 else default


def _model_billing_policy(ai_model: AiModel, expected_type: str) -> Dict[str, Any]:
    policy = model_billing_value(ai_model, "policy") or {}
    return validate_model_billing_policy(
        expected_type,
        policy,
        vendor=getattr(ai_model, "vendor", None),
    )


def _is_apimart_text_credits_policy(
    ai_model: AiModel,
    policy: Dict[str, Any],
) -> bool:
    return (
        ai_model.vendor == APIMART_VENDOR
        and _normalized_text(policy.get("pricing_unit")) == APIMART_TEXT_PRICING_UNIT
    )


def _calculate_apimart_text_usage_points(
    ai_model: AiModel,
    usage: Dict[str, Any],
    *,
    input_tokens: int,
    output_tokens: int,
    policy: Dict[str, Any],
) -> Tuple[int, Dict[str, Any]]:
    cached_tokens = min(_extract_cached_tokens(usage), input_tokens)
    cache_write_tokens = min(
        _extract_cache_write_tokens(usage),
        max(input_tokens - cached_tokens, 0),
    )
    normal_input_tokens = max(
        input_tokens - cached_tokens - cache_write_tokens,
        0,
    )
    tier_index, tier = _select_apimart_text_tier(policy["tiers"], input_tokens)

    input_rate = _decimal(tier["input_credits_per_million"])
    cached_input_rate = _decimal(tier["cached_input_credits_per_million"])
    cache_write_rate = _decimal(tier["cache_write_credits_per_million"])
    output_rate = _decimal(tier["output_credits_per_million"])
    credits_cost = (
        Decimal(normal_input_tokens) * input_rate
        + Decimal(cached_tokens) * cached_input_rate
        + Decimal(cache_write_tokens) * cache_write_rate
        + Decimal(output_tokens) * output_rate
    ) / Decimal("1000000")
    provider_cost_cny = credits_cost * _APIMART_CREDITS_TO_CNY
    provider_cost_points = _ceil_points(provider_cost_cny * _PLATFORM_POINTS_PER_CNY)
    platform_rate = _decimal(model_billing_value(ai_model, "platform"))
    minimum_points = max(0, int(policy.get("minimum_points", 0)))
    user_points_cost = max(
        minimum_points,
        _ceil_points(Decimal(provider_cost_points) * platform_rate),
    )
    user_charge_cny = Decimal(user_points_cost) / _PLATFORM_POINTS_PER_CNY
    gross_profit_cny = user_charge_cny - provider_cost_cny
    gross_margin = gross_profit_cny / user_charge_cny if user_charge_cny > 0 else Decimal("0")
    return user_points_cost, {
        "billing_type": "apimart_usage_credits",
        "billing_source": "provider_usage",
        "pricing_mode": "usage_credits_platform_rate",
        "pricing_unit": APIMART_TEXT_PRICING_UNIT,
        "tier_index": tier_index,
        "tier_max_input_tokens": tier.get("max_input_tokens"),
        "input_tokens": input_tokens,
        "normal_input_tokens": normal_input_tokens,
        "cached_tokens": cached_tokens,
        "cache_write_tokens": cache_write_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": _extract_reasoning_tokens(usage),
        "input_credits_per_million": str(input_rate),
        "cached_input_credits_per_million": str(cached_input_rate),
        "cache_write_credits_per_million": str(cache_write_rate),
        "output_credits_per_million": str(output_rate),
        "provider_credits_cost": str(credits_cost),
        "provider_cost_cny": str(provider_cost_cny),
        "provider_cost_points": provider_cost_points,
        "platform_rate": str(platform_rate),
        "minimum_points": minimum_points,
        "user_points_cost": user_points_cost,
        "user_charge_cny": str(user_charge_cny),
        "gross_profit_cny": str(gross_profit_cny),
        "gross_margin": str(gross_margin),
        "credits_to_cny_rate": str(_APIMART_CREDITS_TO_CNY),
        "points_per_cny": str(_PLATFORM_POINTS_PER_CNY),
    }


def _select_apimart_text_tier(
    tiers: Sequence[Dict[str, Any]],
    input_tokens: int,
) -> Tuple[int, Dict[str, Any]]:
    for index, tier in enumerate(tiers, start=1):
        maximum = tier.get("max_input_tokens")
        if maximum is None or input_tokens <= int(maximum):
            return index, tier
    return len(tiers), tiers[-1]


def _normalized_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _nearest_rank_percentile(values: Sequence[int], percentile: Decimal) -> int:
    rank = (Decimal(len(values)) * percentile).quantize(
        Decimal("1"),
        rounding=ROUND_CEILING,
    )
    return values[max(0, int(rank) - 1)]


def _ceil_points(value: Decimal) -> int:
    return max(0, int(value.quantize(Decimal("1"), rounding=ROUND_CEILING)))


def _decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal("1" if value is None else str(value))
