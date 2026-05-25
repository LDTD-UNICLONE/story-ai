from copy import deepcopy
from typing import Any, Dict, Optional, Set


VOLCENGINE_ARK_VENDOR = "volcengine_ark"
VOLCENGINE_ARK_VIDEO_MODEL_PREFIXES = ("doubao-seedance",)

VOLCENGINE_ARK_VIDEO_CAPABILITIES: Dict[str, Any] = {
    "provider": VOLCENGINE_ARK_VENDOR,
    "model_family": "doubao-seedance",
    "endpoint": "/api/v3/contents/generations/tasks",
    "task_endpoint": "/api/v3/contents/generations/tasks/{task_id}",
    "modes": ["text_to_video", "multimodal_reference", "image_to_video", "first_last_frame"],
    "fields": [
        {"name": "ratio", "type": "select", "label": "画面比例", "required": False, "options": ["16:9", "9:16", "1:1", "adaptive"]},
        {"name": "duration", "type": "integer", "label": "视频时长", "required": False},
        {"name": "resolution", "type": "string", "label": "分辨率", "required": False},
        {"name": "generate_audio", "type": "boolean", "label": "生成音频", "required": False},
        {"name": "return_last_frame", "type": "boolean", "label": "返回尾帧", "required": False},
    ],
    "request_keys": [
        "duration",
        "generate_audio",
        "ratio",
        "resolution",
        "return_last_frame",
    ],
    "media_limits": {"images": 9, "videos": 3, "audios": 3},
    "defaults": {
        "text_ratio": "16:9",
        "reference_ratio": "adaptive",
        "duration": 5,
        "generate_audio": True,
        "watermark": False,
    },
    "supports_async_task": True,
}


def is_volcengine_ark_video_model(model_id: str) -> bool:
    normalized = model_id.lower()
    return any(normalized.startswith(prefix) for prefix in VOLCENGINE_ARK_VIDEO_MODEL_PREFIXES)


def merge_video_capabilities(saved: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    capabilities = deepcopy(VOLCENGINE_ARK_VIDEO_CAPABILITIES)
    if saved:
        for key, value in saved.items():
            if value is not None:
                capabilities[key] = value
    for key in ("defaults", "fields", "media_limits", "modes", "request_keys", "supports_async_task"):
        capabilities[key] = deepcopy(VOLCENGINE_ARK_VIDEO_CAPABILITIES[key])
    return capabilities


def allowed_video_request_keys(saved: Optional[Dict[str, Any]] = None) -> Set[str]:
    return set(merge_video_capabilities(saved).get("request_keys") or [])
