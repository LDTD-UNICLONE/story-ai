"""Validate billing policy configuration without calculation or settlement dependencies."""

from decimal import Decimal
from typing import Any, Dict, Optional

from app.core.exceptions import AppException
from app.integrations.apimart import APIMART_VENDOR


_BILLING_POLICY_TYPES = {"text", "image", "video"}
_VIDEO_REFERENCE_TYPES = {"none", "image", "video", "audio", "multimodal"}
APIMART_TEXT_PRICING_UNIT = "apimart_credits"
_APIMART_TEXT_CREDIT_RATE_KEYS = (
    "input_credits_per_million",
    "cached_input_credits_per_million",
    "cache_write_credits_per_million",
    "output_credits_per_million",
)
_GENERIC_TEXT_POLICY_KEYS = {
    "type",
    "input_points_per_million",
    "cached_input_points_per_million",
    "output_points_per_million",
    "minimum_points",
}
_APIMART_TEXT_POLICY_KEYS = {
    "type",
    "pricing_unit",
    "tiers",
    "minimum_points",
}
_APIMART_TEXT_TIER_KEYS = {
    "max_input_tokens",
    *_APIMART_TEXT_CREDIT_RATE_KEYS,
}
_IMAGE_POLICY_KEYS = {
    "type",
    # Accepted only to normalize legacy configurations. base_points is authoritative.
    "points_per_image",
    "default_resolution",
    "resolution_multipliers",
    "pixel_tiers",
    "free_reference_images",
    "points_per_additional_reference_image",
}
_VIDEO_POLICY_KEYS = {
    "type",
    "default_duration_seconds",
    "default_resolution",
    "rates",
}


def validate_model_billing_policy(
    model_type: str,
    policy: Optional[Dict[str, Any]],
    *,
    vendor: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate and canonicalize an admin-defined points policy."""
    if not policy:
        return {}
    if not isinstance(policy, dict):
        _raise_invalid_billing_policy("计费策略必须是 JSON 对象")

    policy_type = str(policy.get("type") or "").strip().lower()
    if policy_type not in _BILLING_POLICY_TYPES or policy_type != model_type:
        _raise_invalid_billing_policy("计费策略类型必须与模型类型一致")

    if policy_type == "text":
        pricing_unit = str(policy.get("pricing_unit") or "").strip().lower()
        if pricing_unit == APIMART_TEXT_PRICING_UNIT:
            _reject_unknown_policy_keys(policy, _APIMART_TEXT_POLICY_KEYS)
            if vendor is not None and str(vendor or "").strip().lower() != APIMART_VENDOR:
                _raise_invalid_billing_policy("APIMart Credits 计费策略仅支持 APIMart 厂商")
            _validate_apimart_text_credits_policy(policy)
        elif pricing_unit:
            _raise_invalid_billing_policy(f"不支持的文本计费单位：{pricing_unit}")
        else:
            _reject_unknown_policy_keys(policy, _GENERIC_TEXT_POLICY_KEYS)
            for key in (
                "input_points_per_million",
                "cached_input_points_per_million",
                "output_points_per_million",
            ):
                _validate_non_negative_decimal(policy, key, required=True)
            _validate_non_negative_int(policy, "minimum_points", required=False)
    elif policy_type == "image":
        _reject_unknown_policy_keys(policy, _IMAGE_POLICY_KEYS)
        _validate_non_negative_decimal(policy, "points_per_image", required=False)
        multipliers = policy.get("resolution_multipliers", {})
        if not isinstance(multipliers, dict):
            _raise_invalid_billing_policy("resolution_multipliers 必须是 JSON 对象")
        for resolution, value in multipliers.items():
            if not str(resolution).strip():
                _raise_invalid_billing_policy("图片分辨率名称不能为空")
            _validate_decimal_value(value, f"分辨率 {resolution} 的倍率")
        _validate_optional_policy_text(policy, "default_resolution")
        pixel_tiers = policy.get("pixel_tiers", {})
        if not isinstance(pixel_tiers, dict):
            _raise_invalid_billing_policy("pixel_tiers 必须是 JSON 对象")
        for resolution, max_pixels in pixel_tiers.items():
            if not str(resolution).strip():
                _raise_invalid_billing_policy("像素档位名称不能为空")
            _validate_non_negative_int_value(
                max_pixels,
                f"像素档位 {resolution} 的最大像素数",
                positive=True,
            )
        _validate_non_negative_int(policy, "free_reference_images", required=False)
        _validate_non_negative_decimal(
            policy,
            "points_per_additional_reference_image",
            required=False,
        )
    else:
        _reject_unknown_policy_keys(policy, _VIDEO_POLICY_KEYS)
        _validate_non_negative_int(
            policy, "default_duration_seconds", required=False, positive=True
        )
        _validate_optional_policy_text(policy, "default_resolution")
        rates = policy.get("rates")
        if not isinstance(rates, dict) or not rates:
            _raise_invalid_billing_policy("视频计费策略 rates 不能为空")
        for resolution, reference_rates in rates.items():
            if not str(resolution).strip():
                _raise_invalid_billing_policy("视频分辨率名称不能为空")
            if not isinstance(reference_rates, dict) or not reference_rates:
                _raise_invalid_billing_policy(f"分辨率 {resolution} 的参考类型费率不能为空")
            for reference_type, value in reference_rates.items():
                normalized_reference_type = str(reference_type).strip().lower()
                if normalized_reference_type not in _VIDEO_REFERENCE_TYPES:
                    _raise_invalid_billing_policy(f"不支持的视频参考类型：{reference_type}")
                _validate_decimal_value(
                    value,
                    f"分辨率 {resolution}、参考类型 {reference_type} 的费率",
                )
    normalized_policy = dict(policy)
    normalized_policy["type"] = policy_type
    if policy_type == "text" and str(policy.get("pricing_unit") or "").strip().lower():
        normalized_policy["pricing_unit"] = str(policy["pricing_unit"] or "").strip().lower()
    if policy_type == "image":
        normalized_policy.pop("points_per_image", None)
    return normalized_policy


def _validate_apimart_text_credits_policy(policy: Dict[str, Any]) -> None:
    tiers = policy.get("tiers")
    if not isinstance(tiers, list) or not tiers:
        _raise_invalid_billing_policy("APIMart 文本计费策略 tiers 必须是非空数组")
    if len(tiers) > 10:
        _raise_invalid_billing_policy("APIMart 文本计费阶梯不能超过 10 个")

    previous_max = 0
    last_index = len(tiers) - 1
    for index, tier in enumerate(tiers):
        if not isinstance(tier, dict):
            _raise_invalid_billing_policy(f"第 {index + 1} 个计费阶梯必须是 JSON 对象")
        _reject_unknown_policy_keys(tier, _APIMART_TEXT_TIER_KEYS)
        for key in _APIMART_TEXT_CREDIT_RATE_KEYS:
            _validate_non_negative_decimal(tier, key, required=True)

        if index == last_index:
            if "max_input_tokens" in tier:
                _raise_invalid_billing_policy("最后一个 APIMart 文本计费阶梯不能设置输入上限")
            continue

        _validate_non_negative_int(
            tier,
            "max_input_tokens",
            required=True,
            positive=True,
        )
        maximum = int(tier["max_input_tokens"])
        if maximum <= previous_max:
            _raise_invalid_billing_policy("APIMart 文本计费阶梯上限必须严格递增")
        previous_max = maximum

    _validate_non_negative_int(policy, "minimum_points", required=False)


def _validate_non_negative_decimal(
    policy: Dict[str, Any],
    key: str,
    *,
    required: bool,
) -> None:
    if key not in policy:
        if required:
            _raise_invalid_billing_policy(f"缺少计费字段：{key}")
        return
    _validate_decimal_value(policy[key], key)


def _validate_decimal_value(value: Any, field_name: str) -> None:
    if isinstance(value, bool):
        _raise_invalid_billing_policy(f"{field_name} 必须是非负数")
    try:
        number = Decimal(str(value))
    except Exception:
        _raise_invalid_billing_policy(f"{field_name} 必须是非负数")
    if not number.is_finite() or number < 0:
        _raise_invalid_billing_policy(f"{field_name} 必须是非负数")


def _validate_non_negative_int(
    policy: Dict[str, Any],
    key: str,
    *,
    required: bool,
    positive: bool = False,
) -> None:
    if key not in policy:
        if required:
            _raise_invalid_billing_policy(f"缺少计费字段：{key}")
        return
    value = policy[key]
    if isinstance(value, bool):
        _raise_invalid_billing_policy(f"{key} 必须是整数")
    try:
        number = int(value)
    except (TypeError, ValueError):
        _raise_invalid_billing_policy(f"{key} 必须是整数")
    if str(number) != str(value).strip() and not isinstance(value, int):
        _raise_invalid_billing_policy(f"{key} 必须是整数")
    if number < 0 or (positive and number == 0):
        qualifier = "正整数" if positive else "非负整数"
        _raise_invalid_billing_policy(f"{key} 必须是{qualifier}")


def _validate_non_negative_int_value(
    value: Any,
    field_name: str,
    *,
    positive: bool = False,
) -> None:
    if isinstance(value, bool):
        _raise_invalid_billing_policy(f"{field_name} 必须是整数")
    try:
        number = int(value)
    except (TypeError, ValueError):
        _raise_invalid_billing_policy(f"{field_name} 必须是整数")
    if str(number) != str(value).strip() and not isinstance(value, int):
        _raise_invalid_billing_policy(f"{field_name} 必须是整数")
    if number < 0 or (positive and number == 0):
        qualifier = "正整数" if positive else "非负整数"
        _raise_invalid_billing_policy(f"{field_name} 必须是{qualifier}")


def _validate_optional_policy_text(policy: Dict[str, Any], key: str) -> None:
    if key not in policy:
        return
    if not isinstance(policy[key], str) or not policy[key].strip():
        _raise_invalid_billing_policy(f"{key} 必须是非空字符串")


def _raise_invalid_billing_policy(message: str) -> None:
    raise AppException(message, code=40062, status_code=400)


def _reject_unknown_policy_keys(value: Dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        _raise_invalid_billing_policy(f"计费策略包含不支持的字段：{', '.join(unknown)}")
