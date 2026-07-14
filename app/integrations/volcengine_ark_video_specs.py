from copy import deepcopy
from typing import Any, Dict, Optional, Set


VOLCENGINE_ARK_VENDOR = "volcengine_ark"
VOLCENGINE_ARK_VIDEO_MODEL_PREFIXES = ("doubao-seedance",)
VOLCENGINE_ARK_VIDEO_RATIOS = ["adaptive", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"]
VOLCENGINE_ARK_VIDEO_RESOLUTION_ORDER = ("480p", "720p", "1080p", "4k")
VOLCENGINE_ARK_VIDEO_RESOLUTIONS_BY_MODEL = {
    "doubao-seedance-2-0-260128": ["480p", "720p", "1080p", "4k"],
    "doubao-seedance-2-0-fast-260128": ["480p", "720p"],
}
DEFAULT_VIDEO_RESOLUTIONS = ["480p", "720p"]

VOLCENGINE_ARK_VIDEO_CAPABILITIES: Dict[str, Any] = {
    "provider": VOLCENGINE_ARK_VENDOR,
    "model_family": "doubao-seedance",
    "endpoint": "/api/v3/contents/generations/tasks",
    "task_endpoint": "/api/v3/contents/generations/tasks/{task_id}",
    "modes": ["text_to_video", "multimodal_reference", "image_to_video", "first_last_frame"],
    "fields": [
        {"name": "ratio", "type": "select", "label": "画面比例", "required": False, "options": VOLCENGINE_ARK_VIDEO_RATIOS},
        {"name": "duration", "type": "integer", "label": "视频时长", "required": False},
        {"name": "resolution", "type": "select", "label": "分辨率", "required": False, "options": DEFAULT_VIDEO_RESOLUTIONS},
        {"name": "generate_audio", "type": "boolean", "label": "生成音频", "required": False},
        {"name": "return_last_frame", "type": "boolean", "label": "返回尾帧", "required": False},
        {"name": "watermark", "type": "boolean", "label": "水印", "required": False},
    ],
    "request_keys": [
        "callback_url",
        "duration",
        "execution_expires_after",
        "generate_audio",
        "priority",
        "ratio",
        "resolution",
        "return_last_frame",
        "safety_identifier",
        "seed",
        "tools",
        "watermark",
    ],
    "media_limits": {"images": 9, "videos": 3, "audios": 3},
    "defaults": {
        "text_ratio": "adaptive",
        "reference_ratio": "adaptive",
        "duration": 5,
        "resolution": "720p",
        "generate_audio": True,
        "watermark": False,
    },
    "supports_async_task": True,
}


def is_volcengine_ark_video_model(model_id: str) -> bool:
    normalized = model_id.lower()
    return any(normalized.startswith(prefix) for prefix in VOLCENGINE_ARK_VIDEO_MODEL_PREFIXES)


def merge_video_capabilities(
    model_id_or_saved: Optional[Any] = None,
    saved: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    model_id = model_id_or_saved if isinstance(model_id_or_saved, str) else ""
    if saved is None and isinstance(model_id_or_saved, dict):
        saved = model_id_or_saved

    capabilities = deepcopy(VOLCENGINE_ARK_VIDEO_CAPABILITIES)
    resolutions = _supported_resolutions(model_id)
    capabilities["fields"] = _video_fields(resolutions)
    capabilities["defaults"] = {
        **deepcopy(VOLCENGINE_ARK_VIDEO_CAPABILITIES["defaults"]),
        "reference_ratio": "adaptive",
        "resolution": _default_resolution(resolutions),
    }
    capabilities["resolutions"] = resolutions
    capabilities["ratios"] = list(VOLCENGINE_ARK_VIDEO_RATIOS)
    if saved:
        for key, value in saved.items():
            if value is not None:
                capabilities[key] = value
    capabilities["fields"] = _video_fields(resolutions)
    capabilities["defaults"] = {
        **deepcopy(VOLCENGINE_ARK_VIDEO_CAPABILITIES["defaults"]),
        "reference_ratio": "adaptive",
        "resolution": _default_resolution(resolutions),
    }
    capabilities["resolutions"] = resolutions
    capabilities["ratios"] = list(VOLCENGINE_ARK_VIDEO_RATIOS)
    capabilities["media_limits"] = deepcopy(VOLCENGINE_ARK_VIDEO_CAPABILITIES["media_limits"])
    capabilities["modes"] = deepcopy(VOLCENGINE_ARK_VIDEO_CAPABILITIES["modes"])
    capabilities["request_keys"] = deepcopy(VOLCENGINE_ARK_VIDEO_CAPABILITIES["request_keys"])
    capabilities["supports_async_task"] = VOLCENGINE_ARK_VIDEO_CAPABILITIES["supports_async_task"]
    for key in (
        "defaults",
        "fields",
        "media_limits",
        "modes",
        "request_keys",
        "supports_async_task",
        "resolutions",
        "ratios",
    ):
        if key in capabilities:
            capabilities[key] = deepcopy(capabilities[key])
    return capabilities


def allowed_video_request_keys(saved: Optional[Dict[str, Any]] = None) -> Set[str]:
    return set(merge_video_capabilities(saved or {}).get("request_keys") or [])


def normalize_video_resolution(value: Any, capabilities: Optional[Dict[str, Any]] = None) -> str:
    resolutions = _capability_resolutions(capabilities)
    requested = _normalize_resolution(value)
    if requested is None:
        defaults = (capabilities or {}).get("defaults") or {}
        default_resolution = _normalize_resolution(defaults.get("resolution"))
        if default_resolution in resolutions:
            return default_resolution
        return _default_resolution(resolutions)
    if requested in resolutions:
        return requested

    requested_index = VOLCENGINE_ARK_VIDEO_RESOLUTION_ORDER.index(requested)
    indexed = [(VOLCENGINE_ARK_VIDEO_RESOLUTION_ORDER.index(item), item) for item in resolutions]
    lower_or_equal = [item for item in indexed if item[0] <= requested_index]
    if lower_or_equal:
        return max(lower_or_equal)[1]
    return min(indexed)[1]


def is_known_video_resolution(value: Any) -> bool:
    return _normalize_resolution(value) is not None


def is_video_resolution_supported(value: Any, capabilities: Optional[Dict[str, Any]] = None) -> bool:
    requested = _normalize_resolution(value)
    if requested is None:
        return True
    return requested in _capability_resolutions(capabilities)


def _supported_resolutions(model_id: str) -> list[str]:
    normalized = model_id.lower()
    for key, resolutions in VOLCENGINE_ARK_VIDEO_RESOLUTIONS_BY_MODEL.items():
        if normalized == key or normalized.endswith(key) or key in normalized:
            return list(resolutions)
    return list(DEFAULT_VIDEO_RESOLUTIONS)


def _video_fields(resolutions: list[str]) -> list[Dict[str, Any]]:
    fields = deepcopy(VOLCENGINE_ARK_VIDEO_CAPABILITIES["fields"])
    for field in fields:
        if field.get("name") == "resolution":
            field["options"] = list(resolutions)
        elif field.get("name") == "ratio":
            field["options"] = list(VOLCENGINE_ARK_VIDEO_RATIOS)
    return fields


def _default_resolution(resolutions: list[str]) -> str:
    return "720p" if "720p" in resolutions else resolutions[-1]


def _capability_resolutions(capabilities: Optional[Dict[str, Any]]) -> list[str]:
    raw = (capabilities or {}).get("resolutions")
    if isinstance(raw, list):
        normalized = [_normalize_resolution(item) for item in raw]
        values = [item for item in normalized if item in VOLCENGINE_ARK_VIDEO_RESOLUTION_ORDER]
        if values:
            return values
    return list(DEFAULT_VIDEO_RESOLUTIONS)


def _normalize_resolution(value: Any) -> Optional[str]:
    if value is None:
        return None
    normalized = str(value).strip().lower().replace(" ", "")
    aliases = {
        "480": "480p",
        "480p": "480p",
        "720": "720p",
        "720p": "720p",
        "1080": "1080p",
        "1080p": "1080p",
        "1k": "1080p",
        "2160": "4k",
        "2160p": "4k",
        "3840": "4k",
        "3840x2160": "4k",
        "4": "4k",
        "4k": "4k",
        "uhd": "4k",
    }
    return aliases.get(normalized)
