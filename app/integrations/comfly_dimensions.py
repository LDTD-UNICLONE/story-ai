from typing import Any, Dict, Optional, Set, Tuple

from app.core.exceptions import AppException


RATIO_ALIASES = {
    "1": "1:1",
    "1:1": "1:1",
    "square": "1:1",
    "21:9": "21:9",
    "ultrawide": "21:9",
    "16:9": "16:9",
    "landscape": "16:9",
    "9:16": "9:16",
    "portrait": "9:16",
    "4:3": "4:3",
    "3:4": "3:4",
    "3:2": "3:2",
    "2:3": "2:3",
    "9:21": "9:21",
    "keep_ratio": "keep_ratio",
    "adaptive": "adaptive",
}

COMFLY_IMAGE_SIZE_BY_RATIO = {
    "1k": {
        "1:1": "1024x1024",
        "3:2": "1536x1024",
        "2:3": "1024x1536",
        "16:9": "1792x1024",
        "9:16": "1024x1792",
        "4:3": "1344x1024",
        "3:4": "1024x1344",
    },
    "2k": {
        "1:1": "2048x2048",
        "3:2": "2304x1536",
        "2:3": "1536x2304",
        "16:9": "2048x1152",
        "9:16": "1152x2048",
        "4:3": "2048x1536",
        "3:4": "1536x2048",
    },
    "3k": {
        "1:1": "3072x3072",
        "3:2": "3072x2048",
        "2:3": "2048x3072",
        "16:9": "3072x1728",
        "9:16": "1728x3072",
        "4:3": "3072x2304",
        "3:4": "2304x3072",
    },
    "4k": {
        "16:9": "3840x2160",
        "9:16": "2160x3840",
    },
}

DEFAULT_RATIO_BY_IMAGE_QUALITY = {
    "1k": "1:1",
    "2k": "1:1",
    "3k": "1:1",
    "4k": "16:9",
}

IMAGE_QUALITY_ORDER = ("1k", "2k", "3k", "4k")

IMAGE_MODEL_SIZE_POLICIES = {
    "gpt-image-2-all": {
        "qualities": ("1k",),
        "default_quality": "1k",
        "allow_custom_size": False,
    },
    "gpt-image-2": {
        "qualities": ("2k", "4k"),
        "default_quality": "2k",
        "allow_custom_size": True,
        "fallback_quality": "2k",
    },
    "doubao-seedream-5-0-260128": {
        "qualities": ("2k", "3k", "4k"),
        "default_quality": "2k",
        "allow_custom_size": False,
    },
    "doubao-seedream-4-5-251128": {
        "qualities": ("2k", "4k"),
        "default_quality": "2k",
        "allow_custom_size": False,
    },
}

DEFAULT_IMAGE_SIZE_POLICY = {
    "qualities": ("1k", "2k", "4k"),
    "default_quality": "2k",
    "allow_custom_size": False,
}

VIDEO_SIZE_BY_RATIO = {
    "21:9": "1680x720",
    "1:1": "1024x1024",
    "16:9": "1280x720",
    "9:16": "720x1280",
    "4:3": "960x720",
    "3:4": "720x960",
}

IMAGE_ASPECT_RATIO_MODELS = ("flux", "recraft", "ideogram")


def normalize_ratio(value: Any) -> Optional[str]:
    if value is None:
        return None
    ratio = str(value).strip().lower().replace("*", ":").replace("x", ":")
    return RATIO_ALIASES.get(ratio)


def adapt_image_dimensions(model: str, payload: Dict[str, Any], extra: Dict[str, Any]) -> None:
    ratio = normalize_ratio(extra.get("aspect_ratio") or extra.get("ratio"))
    policy = _image_size_policy(model)
    requested_size = _normalize_size(payload.get("size") or extra.get("image_size") or extra.get("resolution"))
    if requested_size and _is_custom_size_allowed(policy, requested_size):
        payload["size"] = requested_size
        payload.pop("aspect_ratio", None)
        return
    if requested_size:
        raise AppException(
            _unsupported_image_parameter_message(requested_size=requested_size),
            code=40016,
            status_code=400,
        )

    if _is_gpt_image_model(model):
        size = _model_image_size(policy, ratio, extra)
        if size:
            payload["size"] = size
            payload.pop("aspect_ratio", None)
        return

    if not ratio:
        size = _model_image_size(policy, None, extra)
        if size:
            payload["size"] = size
            payload.pop("aspect_ratio", None)
        return
    if _image_model_prefers_aspect_ratio(model):
        payload["aspect_ratio"] = ratio
        return

    size = _model_image_size(policy, ratio, extra)
    if size:
        payload["size"] = size
        payload.pop("aspect_ratio", None)
        return

    raise AppException(
        _unsupported_image_parameter_message(ratio=ratio, quality=_normalize_quality(extra.get("quality") or extra.get("resolution") or extra.get("image_quality"))),
        code=40016,
        status_code=400,
    )


def adapt_video_dimensions(
    payload: Dict[str, Any],
    extra: Dict[str, Any],
    allowed_keys: Set[str],
) -> None:
    ratio = normalize_ratio(extra.get("aspect_ratio") or extra.get("ratio"))
    if not ratio:
        return

    if "aspect_ratio" in allowed_keys:
        payload["aspect_ratio"] = ratio
        return

    if "size" in allowed_keys and "size" not in payload:
        size = VIDEO_SIZE_BY_RATIO.get(ratio)
        if size:
            payload["size"] = size


def _image_model_prefers_aspect_ratio(model: str) -> bool:
    normalized = model.lower()
    return any(token in normalized for token in IMAGE_ASPECT_RATIO_MODELS)


def _is_gpt_image_model(model: str) -> bool:
    normalized = model.lower()
    return "gpt" in normalized and ("image" in normalized or "img" in normalized)


def _image_size_policy(model: str) -> Dict[str, Any]:
    normalized = model.strip().lower()
    if normalized in IMAGE_MODEL_SIZE_POLICIES:
        return IMAGE_MODEL_SIZE_POLICIES[normalized]
    for model_id, policy in IMAGE_MODEL_SIZE_POLICIES.items():
        if normalized.endswith(model_id) or model_id in normalized:
            return policy
    return DEFAULT_IMAGE_SIZE_POLICY


def _model_image_size(
    policy: Dict[str, Any],
    ratio: Optional[str],
    extra: Dict[str, Any],
) -> Optional[str]:
    quality = _select_image_quality(policy, extra)
    target_ratio = ratio or DEFAULT_RATIO_BY_IMAGE_QUALITY.get(quality, "1:1")
    size = COMFLY_IMAGE_SIZE_BY_RATIO.get(quality, {}).get(target_ratio)
    if size:
        return size

    fallback_quality = policy.get("fallback_quality")
    if fallback_quality:
        size = COMFLY_IMAGE_SIZE_BY_RATIO.get(str(fallback_quality), {}).get(target_ratio)
        if size:
            return size
    return None


def _unsupported_image_parameter_message(
    *,
    requested_size: Optional[str] = None,
    ratio: Optional[str] = None,
    quality: Optional[str] = None,
) -> str:
    if requested_size:
        return f"当前模型不支持所选图片尺寸：{requested_size}，请调整尺寸或选择其它模型"
    if ratio and quality:
        return f"当前模型不支持所选比例和清晰度组合：{ratio} + {quality}，请调整参数后重试"
    if ratio:
        return f"当前模型不支持所选图片比例：{ratio}，请调整比例后重试"
    if quality:
        return f"当前模型不支持所选清晰度：{quality}，请调整清晰度后重试"
    return "当前模型不支持所选参数组合，请调整参数后重试"


def _select_image_quality(policy: Dict[str, Any], extra: Dict[str, Any]) -> str:
    requested_quality = _normalize_quality(extra.get("quality") or extra.get("resolution") or extra.get("image_quality"))
    supported = tuple(policy["qualities"])
    if requested_quality is None:
        return str(policy["default_quality"])
    if requested_quality in supported:
        return requested_quality

    requested_index = IMAGE_QUALITY_ORDER.index(requested_quality)
    supported_with_indexes: Tuple[Tuple[int, str], ...] = tuple(
        (IMAGE_QUALITY_ORDER.index(quality), quality) for quality in supported
    )
    higher_or_equal = [item for item in supported_with_indexes if item[0] >= requested_index]
    if higher_or_equal:
        return min(higher_or_equal)[1]
    return max(supported_with_indexes)[1]


def _normalize_quality(value: Any) -> Optional[str]:
    if value is None:
        return None
    normalized = str(value).strip().lower().replace(" ", "")
    aliases = {
        "1": "1k",
        "1k": "1k",
        "1024": "1k",
        "2": "2k",
        "2k": "2k",
        "2048": "2k",
        "3": "3k",
        "3k": "3k",
        "3072": "3k",
        "4": "4k",
        "4k": "4k",
        "3840": "4k",
        "4096": "4k",
    }
    return aliases.get(normalized)


def _is_custom_size_allowed(policy: Dict[str, Any], requested_size: str) -> bool:
    if policy.get("allow_custom_size"):
        return True
    return any(
        requested_size in COMFLY_IMAGE_SIZE_BY_RATIO.get(quality, {}).values()
        for quality in policy["qualities"]
    )


def _normalize_size(value: Any) -> Optional[str]:
    if value is None:
        return None
    normalized = str(value).strip().lower().replace("*", "x")
    if "x" not in normalized:
        return None
    width, height = normalized.split("x", 1)
    if not width.isdigit() or not height.isdigit():
        return None
    return f"{int(width)}x{int(height)}"
