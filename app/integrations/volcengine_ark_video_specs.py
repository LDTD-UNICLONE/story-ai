from copy import deepcopy
from typing import Any, Dict, Optional, Set

from app.core.exceptions import AppException


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
        {
            "name": "ratio", "type": "select", "label": "画面比例", "required": False, "options": VOLCENGINE_ARK_VIDEO_RATIOS,
        },
        {"name": "duration", "type": "integer", "label": "视频时长", "required": False},
        {
            "name": "resolution", "type": "select", "label": "分辨率", "required": False, "options": DEFAULT_VIDEO_RESOLUTIONS,
        },
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


ARK_REFERENCE_VIDEO_CONTENT_TYPES = {"video/mp4", "video/quicktime"}
ARK_REFERENCE_VIDEO_CODECS = {"h264", "hevc", "h265"}
ARK_REFERENCE_VIDEO_AUDIO_CODECS = {"aac", "mp3"}
ARK_REFERENCE_VIDEO_MIN_DURATION_SECONDS = 2
ARK_REFERENCE_VIDEO_MAX_DURATION_SECONDS = 15
ARK_REFERENCE_VIDEO_MAX_TOTAL_DURATION_SECONDS = 15
ARK_REFERENCE_VIDEO_MAX_ACCEPTED_DURATION_SECONDS = 15.2
ARK_REFERENCE_VIDEO_MAX_ACCEPTED_TOTAL_DURATION_SECONDS = 15.2
ARK_REFERENCE_VIDEO_DURATION_TOLERANCE_SECONDS = 0.2
ARK_REFERENCE_VIDEO_MAX_SIZE_BYTES = 200 * 1024 * 1024
ARK_REFERENCE_VIDEO_MIN_FPS = 24
ARK_REFERENCE_VIDEO_MAX_FPS = 60
ARK_REFERENCE_VIDEO_MIN_SIDE_PX = 300
ARK_REFERENCE_VIDEO_MAX_SIDE_PX = 6000
ARK_REFERENCE_VIDEO_MIN_PIXELS = 640 * 640
ARK_REFERENCE_VIDEO_MAX_PIXELS = 3326 * 2494
ARK_REFERENCE_VIDEO_MIN_RATIO = 0.4
ARK_REFERENCE_VIDEO_MAX_RATIO = 2.5


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


def is_video_resolution_supported(
    value: Any, capabilities: Optional[Dict[str, Any]] = None
) -> bool:
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


def validate_reference_video_file(item: Dict[str, Any]) -> None:
    content_type = str(item.get("content_type") or item.get("mime_type") or "").strip().lower()
    if content_type and content_type not in ARK_REFERENCE_VIDEO_CONTENT_TYPES:
        raise AppException("火山方舟参考视频仅支持 mp4 或 mov 格式", code=40012, status_code=400)

    size = _optional_float(item.get("size"))
    if size is not None and size > ARK_REFERENCE_VIDEO_MAX_SIZE_BYTES:
        raise AppException("火山方舟参考视频单个文件不能超过 200MB", code=40012, status_code=400)


def validate_reference_video_metadata(media_info: Dict[str, Any]) -> Optional[float]:
    duration = _optional_float(media_info.get("duration_seconds") or media_info.get("duration"))
    if duration is not None and not _is_ark_reference_video_duration_supported(duration):
        raise AppException(
            f"火山方舟参考视频单个时长必须在 2-15.2 秒之间，当前检测为 {_format_seconds(duration)} 秒",
            code=40012,
            status_code=400,
        )

    video_codec = (
        str(media_info.get("video_codec") or media_info.get("codec_name") or "").strip().lower()
    )
    if video_codec and video_codec not in ARK_REFERENCE_VIDEO_CODECS:
        raise AppException("火山方舟参考视频编码仅支持 H.264/H.265", code=40012, status_code=400)

    audio_codec = str(media_info.get("audio_codec") or "").strip().lower()
    if audio_codec and audio_codec not in ARK_REFERENCE_VIDEO_AUDIO_CODECS:
        raise AppException("火山方舟参考视频音频编码仅支持 AAC/MP3", code=40012, status_code=400)

    fps = _optional_float(media_info.get("fps") or media_info.get("frame_rate"))
    if fps is not None and not (ARK_REFERENCE_VIDEO_MIN_FPS <= fps <= ARK_REFERENCE_VIDEO_MAX_FPS):
        raise AppException("火山方舟参考视频帧率必须在 24-60 FPS 之间", code=40012, status_code=400)

    width = _optional_float(media_info.get("width"))
    height = _optional_float(media_info.get("height"))
    if width is not None and height is not None:
        _validate_ark_reference_video_dimensions(width, height)

    return duration


def validate_reference_video_total_duration(total_duration: float) -> None:
    if total_duration > ARK_REFERENCE_VIDEO_MAX_ACCEPTED_TOTAL_DURATION_SECONDS:
        raise AppException(
            f"火山方舟参考视频总时长不能超过 15.2 秒，当前检测为 {_format_seconds(total_duration)} 秒",
            code=40012,
            status_code=400,
        )


def _validate_ark_reference_video_dimensions(width: float, height: float) -> None:
    if width <= 0 or height <= 0:
        return
    if not (
        ARK_REFERENCE_VIDEO_MIN_SIDE_PX <= width <= ARK_REFERENCE_VIDEO_MAX_SIDE_PX
        and ARK_REFERENCE_VIDEO_MIN_SIDE_PX <= height <= ARK_REFERENCE_VIDEO_MAX_SIDE_PX
    ):
        raise AppException(
            "火山方舟参考视频宽高长度必须在 300-6000px 之间", code=40012, status_code=400
        )

    pixels = width * height
    if not (ARK_REFERENCE_VIDEO_MIN_PIXELS <= pixels <= ARK_REFERENCE_VIDEO_MAX_PIXELS):
        raise AppException("火山方舟参考视频总像素数不符合要求", code=40012, status_code=400)

    ratio = width / height
    if not (ARK_REFERENCE_VIDEO_MIN_RATIO <= ratio <= ARK_REFERENCE_VIDEO_MAX_RATIO):
        raise AppException("火山方舟参考视频宽高比必须在 0.4-2.5 之间", code=40012, status_code=400)


def _is_ark_reference_video_duration_supported(duration: float) -> bool:
    return (
        duration >= ARK_REFERENCE_VIDEO_MIN_DURATION_SECONDS - ARK_REFERENCE_VIDEO_DURATION_TOLERANCE_SECONDS
        and duration <= ARK_REFERENCE_VIDEO_MAX_ACCEPTED_DURATION_SECONDS
    )


def _format_seconds(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _optional_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
