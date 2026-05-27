from __future__ import annotations

import re
from typing import Any


INTERNAL_EXTRA_KEYS = {
    "raw_failed_reason",
    "provider_response",
}

_VENDOR_REPLACEMENTS = (
    (re.compile(r"comfly", re.IGNORECASE), "模型服务"),
    (re.compile(r"volcengine[_-]?ark", re.IGNORECASE), "模型服务"),
    (re.compile(r"\bark\b", re.IGNORECASE), "模型服务"),
    (re.compile(r"VOLCENGINE_ARK", re.IGNORECASE), "模型服务"),
    (re.compile(r"火山方舟|火山引擎|方舟"), "模型服务"),
    (re.compile(r"厂商"), "模型"),
)


def sanitize_public_message(value: Any, fallback: str = "模型响应失败，请稍后再试") -> Any:
    if not isinstance(value, str):
        return value

    message = value.strip()
    if not message:
        return fallback

    for pattern, replacement in _VENDOR_REPLACEMENTS:
        message = pattern.sub(replacement, message)
    message = message.replace("模型服务多模态", "多模态")
    message = message.replace("模型模型", "模型")
    message = message.replace("模型服务 SDK", "模型服务")
    message = message.replace("模型服务 未", "模型服务未")

    lower_message = message.lower()
    if _looks_like_raw_provider_error(lower_message):
        return _generic_model_error(message)
    return message


def sanitize_public_data(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            if key in INTERNAL_EXTRA_KEYS:
                continue
            sanitized[key] = sanitize_public_data(item)
        return sanitized
    if isinstance(value, list):
        return [sanitize_public_data(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_public_data(item) for item in value]
    if isinstance(value, str):
        return sanitize_public_message(value)
    return value


def _looks_like_raw_provider_error(message: str) -> bool:
    raw_tokens = (
        "http 400",
        "http 401",
        "http 403",
        "http 429",
        "http 500",
        "http 502",
        "http 503",
        "error code:",
        '"error"',
        "{'error'",
        "api_key",
        "traceid",
        "request id",
        "request_id",
        "badrequest",
        "invalidparameter",
        "new_api_error",
        "bad_response_status_code",
    )
    return any(token in message for token in raw_tokens)


def _generic_model_error(message: str) -> str:
    lower_message = message.lower()
    if any(token in lower_message for token in ("timeout", "超时")):
        return "模型响应超时，请稍后再试"
    if any(token in lower_message for token in ("系统繁忙", "busy", "rate limit", "http 429")):
        return "模型服务繁忙，请稍后再试"
    if any(token in lower_message for token in ("最多支持 9 张图片", "9 张图片")):
        return "参考图片最多支持 9 张"
    if any(token in lower_message for token in ("最多支持 3 个视频", "3 个视频")):
        return "参考视频最多支持 3 个"
    if any(token in lower_message for token in ("最多支持 3 个音频", "3 个音频")):
        return "参考音频最多支持 3 个"
    if any(token in lower_message for token in ("sensitive", "privacy", "real person", "敏感")):
        return "输入内容未通过模型安全校验，请更换内容后重试"
    if any(token in lower_message for token in ("not valid", "invalid", "参数")):
        return "当前模型不支持所选参数组合，请调整参数后重试"
    return "模型响应失败，请稍后再试"
