from copy import deepcopy
from typing import Any, Dict, List, Optional, Set


def _field(
    name: str,
    field_type: str,
    label: str,
    *,
    required: bool = False,
    options: Optional[List[Any]] = None,
    description: str = "",
    multiple: bool = False,
) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "name": name,
        "type": field_type,
        "label": label,
        "required": required,
        "description": description,
    }
    if options is not None:
        data["options"] = options
    if multiple:
        data["multiple"] = True
    return data


COMMON_VIDEO_RATIOS = ["16:9", "9:16", "1:1", "4:3", "3:4"]
SEEDANCE_RATIOS = ["21:9", "16:9", "4:3", "1:1", "3:4", "9:16", "9:21", "keep_ratio", "adaptive"]
GROK_RATIOS = ["2:3", "3:2", "1:1", "16:9", "9:16"]

DEFAULT_COMFLY_VIDEO_CAPABILITIES: Dict[str, Any] = {
    "provider": "comfly",
    "model_family": "unified_video",
    "endpoint": "/v2/videos/generations",
    "task_endpoint": "/v2/videos/generations/{task_id}",
    "modes": ["text_to_video", "image_to_video", "video_to_video"],
    "fields": [
        _field("aspect_ratio", "select", "画面比例", options=COMMON_VIDEO_RATIOS),
        _field("duration", "integer", "视频时长"),
        _field("size", "string", "视频尺寸"),
        _field("resolution", "string", "分辨率"),
        _field("images", "file_list", "参考图", multiple=True),
        _field("videos", "file_list", "参考视频", multiple=True),
        _field("watermark", "boolean", "水印"),
    ],
    "request_keys": ["aspect_ratio", "duration", "images", "resolution", "size", "videos", "watermark"],
    "media_limits": {},
    "ratios": list(COMMON_VIDEO_RATIOS),
    "supports_async_task": True,
}


COMFLY_VIDEO_SPECS: Dict[str, Dict[str, Any]] = {
    "sora2": {
        "match_prefixes": ["sora-2", "sora2"],
        "capabilities": {
            "provider": "comfly",
            "model_family": "sora2",
            "endpoint": "/v2/videos/generations",
            "task_endpoint": "/v2/videos/generations/{task_id}",
            "modes": ["text_to_video", "image_to_video", "storyboard", "character_consistency"],
            "fields": [
                _field("aspect_ratio", "select", "画面比例", options=["16:9", "9:16"]),
                _field("hd", "boolean", "高清视频"),
                _field("duration", "select", "视频时长", options=["4", "8", "10", "12", "15", "25"]),
                _field("images", "file_list", "参考图", multiple=True),
                _field("character_url", "url", "角色视频地址"),
                _field("character_timestamps", "array", "角色时间戳"),
                _field("notify_hook", "url", "回调地址"),
                _field("watermark", "boolean", "水印"),
                _field("private", "boolean", "私有任务"),
            ],
            "request_keys": [
                "aspect_ratio",
                "duration",
                "hd",
                "images",
                "character_timestamps",
                "character_url",
                "notify_hook",
                "private",
                "watermark",
            ],
            "media_limits": {"images": 1, "character_url": 1},
            "ratios": ["16:9", "9:16"],
            "durations": ["4", "8", "10", "12", "15", "25"],
            "supports_async_task": True,
        },
    },
    "veo": {
        "match_prefixes": ["veo"],
        "capabilities": {
            "provider": "comfly",
            "model_family": "veo",
            "endpoint": "/v2/videos/generations",
            "task_endpoint": "/v2/videos/generations/{task_id}",
            "modes": ["text_to_video", "image_to_video"],
            "fields": [
                _field("aspect_ratio", "select", "画面比例", options=["16:9", "9:16"]),
                _field("duration", "integer", "视频时长"),
                _field("enhance_prompt", "boolean", "提示词增强"),
                _field("enable_upsample", "boolean", "增强分辨率"),
                _field("images", "file_list", "参考图", multiple=True),
            ],
            "request_keys": ["aspect_ratio", "duration", "enable_upsample", "enhance_prompt", "images"],
            "media_limits": {"images": 3},
            "ratios": ["16:9", "9:16"],
            "supports_async_task": True,
        },
    },
    "wan": {
        "match_prefixes": ["wan", "wanx"],
        "capabilities": {
            "provider": "comfly",
            "model_family": "wan",
            "endpoint": "/v2/videos/generations",
            "task_endpoint": "/v2/videos/generations/{task_id}",
            "modes": ["text_to_video", "image_to_video", "first_last_frame", "audio_video"],
            "fields": [
                _field("images", "file_list", "参考图", multiple=True),
                _field("audio_url", "file", "音频文件"),
                _field("size", "string", "视频尺寸"),
                _field("resolution", "string", "分辨率"),
                _field("prompt_extend", "boolean", "提示词扩展"),
                _field("negative_prompt", "string", "负面提示词"),
                _field("seed", "integer", "随机种子"),
                _field("watermark", "boolean", "水印"),
                _field("duration", "integer", "视频时长"),
            ],
            "request_keys": [
                "audio_url",
                "duration",
                "images",
                "negative_prompt",
                "prompt_extend",
                "resolution",
                "seed",
                "size",
                "watermark",
            ],
            "media_limits": {"images": 2, "audio": 1},
            "resolutions": ["480P", "720P", "1080P"],
            "supports_async_task": True,
        },
    },
    "seedance": {
        "match_prefixes": ["seedance", "doubao-seedance"],
        "capabilities": {
            "provider": "comfly",
            "model_family": "seedance",
            "endpoint": "/v2/videos/generations",
            "task_endpoint": "/v2/videos/generations/{task_id}",
            "modes": ["text_to_video", "image_to_video", "first_last_frame"],
            "fields": [
                _field("ratio", "select", "画面比例", options=SEEDANCE_RATIOS),
                _field("duration", "integer", "视频时长"),
                _field("resolution", "select", "分辨率", options=["480p", "720p", "1080p"]),
                _field("images", "file_list", "参考图", multiple=True),
                _field("generate_audio", "boolean", "生成音频"),
                _field("return_last_frame", "boolean", "返回尾帧"),
                _field("camerafixed", "boolean", "固定摄像头"),
                _field("seed", "integer", "随机种子"),
                _field("watermark", "boolean", "水印"),
            ],
            "request_keys": [
                "camerafixed",
                "duration",
                "generate_audio",
                "images",
                "ratio",
                "resolution",
                "return_last_frame",
                "seed",
                "watermark",
            ],
            "media_limits": {"images": 2},
            "ratios": list(SEEDANCE_RATIOS),
            "resolutions": ["480p", "720p", "1080p"],
            "durations": [5, 10],
            "supports_async_task": True,
        },
    },
    "grok": {
        "match_prefixes": ["grok-video"],
        "capabilities": {
            "provider": "comfly",
            "model_family": "grok",
            "endpoint": "/v2/videos/generations",
            "task_endpoint": "/v2/videos/generations/{task_id}",
            "modes": ["text_to_video", "image_to_video", "multimodal_reference"],
            "fields": [
                _field("ratio", "select", "画面比例", options=GROK_RATIOS),
                _field("resolution", "select", "分辨率", options=["720P", "1080P"]),
                _field("duration", "select", "视频时长", options=[6, 10]),
                _field("images", "file_list", "参考图", multiple=True),
            ],
            "request_keys": ["duration", "images", "ratio", "resolution"],
            "media_limits": {"images": 7},
            "ratios": list(GROK_RATIOS),
            "resolutions": ["720P", "1080P"],
            "durations": [6, 10],
            "supports_async_task": True,
        },
    },
}


def build_comfly_video_capabilities(model_id: str) -> Dict[str, Any]:
    normalized = model_id.lower()
    for spec in COMFLY_VIDEO_SPECS.values():
        if any(normalized.startswith(prefix) for prefix in spec["match_prefixes"]):
            return deepcopy(spec["capabilities"])
    return deepcopy(DEFAULT_COMFLY_VIDEO_CAPABILITIES)


def merge_video_capabilities(model_id: str, saved: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    inferred = build_comfly_video_capabilities(model_id)
    if not saved:
        return inferred
    merged = deepcopy(inferred)
    for key, value in saved.items():
        if value is not None:
            merged[key] = value
    if inferred:
        for key in (
            "durations",
            "fields",
            "media_limits",
            "modes",
            "ratios",
            "request_keys",
            "resolutions",
            "supports_async_task",
        ):
            if key in inferred:
                merged[key] = inferred[key]
    return merged


def allowed_video_request_keys(model_id: str, saved: Optional[Dict[str, Any]] = None) -> Set[str]:
    capabilities = merge_video_capabilities(model_id, saved)
    return set(capabilities.get("request_keys") or [])
