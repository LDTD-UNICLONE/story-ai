from copy import deepcopy
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from typing import Any, Dict, Mapping, Optional

from app.core.exceptions import AppException
from app.integrations.apimart import APIMART_VENDOR


MODEL_CONFIGURATION_VERSION = 1
MODEL_OPERATION_STATUSES = {"active", "maintenance"}
_CONFIGURATION_KEYS = {"version", "request", "billing", "operations"}
_REQUEST_KEYS = {"capabilities"}
_BILLING_KEYS = {"base_points", "multipliers", "policy"}
_MULTIPLIER_KEYS = {"model", "cache", "completion", "platform"}
_OPERATIONS_KEYS = {"status", "maintenance_message"}
_APIMART_DEFAULT_PLATFORM_RATES = {
    "text": Decimal("1.2"),
    "image": Decimal("1"),
    "video": Decimal("1.2"),
}


def default_model_platform_multiplier(vendor: str, model_type: str) -> Decimal:
    if vendor == APIMART_VENDOR:
        return _APIMART_DEFAULT_PLATFORM_RATES.get(model_type, Decimal("1"))
    return Decimal("1")


def build_model_configuration(ai_model: Any) -> Dict[str, Any]:
    return normalize_model_configuration(
        vendor=str(getattr(ai_model, "vendor", "")),
        model_type=str(getattr(ai_model, "model_type", "")),
        configuration=getattr(ai_model, "configuration", None) or {},
    )


def build_model_runtime_snapshot(ai_model: Any) -> SimpleNamespace:
    return SimpleNamespace(
        id=getattr(ai_model, "id", None),
        model_id=ai_model.model_id,
        vendor=ai_model.vendor,
        model_type=ai_model.model_type,
        nickname=getattr(ai_model, "nickname", ai_model.model_id),
        configuration=build_model_configuration(ai_model),
    )


def normalize_model_configuration(
    *,
    vendor: str,
    model_type: str,
    configuration: Optional[Mapping[str, Any]],
    base_configuration: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    raw = _require_mapping(configuration or {}, "configuration")
    _reject_unknown_keys(raw, _CONFIGURATION_KEYS, "configuration")
    version = raw.get("version", MODEL_CONFIGURATION_VERSION)
    if isinstance(version, bool) or version != MODEL_CONFIGURATION_VERSION:
        _raise_invalid_configuration(f"仅支持模型配置版本 {MODEL_CONFIGURATION_VERSION}")

    base = _default_configuration(vendor, model_type)
    if base_configuration:
        base = _merge_configuration(base, base_configuration)
    merged = _merge_configuration(base, raw)

    request = _require_mapping(merged["request"], "request")
    _reject_unknown_keys(request, _REQUEST_KEYS, "request")
    capabilities = _require_mapping(request.get("capabilities", {}), "capabilities")

    billing = _require_mapping(merged["billing"], "billing")
    _reject_unknown_keys(billing, _BILLING_KEYS, "billing")
    base_points = _non_negative_int(billing.get("base_points", 0), "base_points")
    multipliers = _require_mapping(billing.get("multipliers", {}), "multipliers")
    _reject_unknown_keys(multipliers, _MULTIPLIER_KEYS, "multipliers")
    normalized_multipliers = {
        key: _non_negative_decimal_text(multipliers.get(key, "1"), key)
        for key in ("model", "cache", "completion", "platform")
    }
    policy = _require_mapping(billing.get("policy", {}), "policy")
    if policy:
        from app.services.model_points import validate_model_billing_policy

        policy = validate_model_billing_policy(
            model_type,
            dict(policy),
            vendor=vendor,
        )

    operations = _require_mapping(merged["operations"], "operations")
    _reject_unknown_keys(operations, _OPERATIONS_KEYS, "operations")
    status = str(operations.get("status") or "active").strip().lower()
    if status not in MODEL_OPERATION_STATUSES:
        _raise_invalid_configuration("operations.status 仅支持 active 或 maintenance")
    maintenance_message = operations.get("maintenance_message")
    if maintenance_message is not None:
        if not isinstance(maintenance_message, str):
            _raise_invalid_configuration("maintenance_message 必须是字符串或 null")
        maintenance_message = maintenance_message.strip() or None
        if maintenance_message and len(maintenance_message) > 500:
            _raise_invalid_configuration("maintenance_message 不能超过 500 个字符")

    return {
        "version": MODEL_CONFIGURATION_VERSION,
        "request": {"capabilities": deepcopy(dict(capabilities))},
        "billing": {
            "base_points": base_points,
            "multipliers": normalized_multipliers,
            "policy": deepcopy(dict(policy)),
        },
        "operations": {
            "status": status,
            "maintenance_message": maintenance_message,
        },
    }


def model_billing_value(ai_model: Any, key: str) -> Any:
    billing = build_model_configuration(ai_model)["billing"]
    if key in {"base_points", "policy"}:
        return billing[key]
    if key in _MULTIPLIER_KEYS:
        return billing["multipliers"][key]
    raise KeyError(key)


def model_request_capabilities(ai_model: Any) -> Dict[str, Any]:
    return build_model_configuration(ai_model)["request"]["capabilities"]


def model_is_available(ai_model: Any) -> bool:
    return build_model_configuration(ai_model)["operations"]["status"] == "active"


def ensure_model_available(ai_model: Any) -> None:
    operations = build_model_configuration(ai_model)["operations"]
    if operations["status"] == "active":
        return
    raise AppException(
        operations.get("maintenance_message") or "模型维护中，请稍后再试",
        code=50301,
        status_code=503,
    )


def _default_configuration(vendor: str, model_type: str) -> Dict[str, Any]:
    return {
        "version": MODEL_CONFIGURATION_VERSION,
        "request": {"capabilities": {}},
        "billing": {
            "base_points": 0,
            "multipliers": {
                "model": "1",
                "cache": "1",
                "completion": "1",
                "platform": str(default_model_platform_multiplier(vendor, model_type)),
            },
            "policy": {},
        },
        "operations": {
            "status": "active",
            "maintenance_message": None,
        },
    }


def _merge_configuration(
    base: Mapping[str, Any],
    override: Mapping[str, Any],
) -> Dict[str, Any]:
    result = deepcopy(dict(base))
    for section in ("request", "billing", "operations"):
        if section not in override:
            continue
        incoming = _require_mapping(override[section], section)
        current = dict(result.get(section) or {})
        current.update(deepcopy(dict(incoming)))
        if section == "billing" and "multipliers" in incoming:
            current_multipliers = dict(
                (result.get("billing") or {}).get("multipliers") or {}
            )
            incoming_multipliers = _require_mapping(
                incoming["multipliers"], "multipliers"
            )
            current_multipliers.update(dict(incoming_multipliers))
            current["multipliers"] = current_multipliers
        result[section] = current
    result["version"] = override.get("version", result.get("version"))
    return result


def _require_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _raise_invalid_configuration(f"{field_name} 必须是 JSON 对象")
    return value


def _reject_unknown_keys(
    value: Mapping[str, Any],
    allowed: set[str],
    field_name: str,
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        _raise_invalid_configuration(
            f"{field_name} 包含不支持的字段：{', '.join(unknown)}"
        )


def _non_negative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        _raise_invalid_configuration(f"{field_name} 必须是非负整数")
    try:
        number = int(value)
    except (TypeError, ValueError):
        _raise_invalid_configuration(f"{field_name} 必须是非负整数")
    if number < 0 or (not isinstance(value, int) and str(number) != str(value).strip()):
        _raise_invalid_configuration(f"{field_name} 必须是非负整数")
    return number


def _non_negative_decimal_text(value: Any, field_name: str) -> str:
    if isinstance(value, bool):
        _raise_invalid_configuration(f"{field_name} 必须是非负数")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        _raise_invalid_configuration(f"{field_name} 必须是非负数")
    if not number.is_finite() or number < 0:
        _raise_invalid_configuration(f"{field_name} 必须是非负数")
    return str(number)


def _raise_invalid_configuration(message: str) -> None:
    raise AppException(message, code=40064, status_code=400)
