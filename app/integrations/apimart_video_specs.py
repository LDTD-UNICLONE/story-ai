from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Optional, Sequence
from urllib.parse import urlsplit

from app.core.exceptions import AppException


@dataclass(frozen=True)
class VideoModelSpec:
    family: str
    ratios: FrozenSet[str]
    resolutions: FrozenSet[str]
    default_resolution: str
    max_images: int
    max_videos: int
    max_audios: int
    duration_min: int
    duration_max: int
    duration_default: Optional[int]
    duration_controllable: bool = True
    duration_special: tuple[int, ...] = ()
    duration_mode_values: tuple[tuple[str, tuple[int, ...]], ...] = ()
    allow_audio_only: bool = False


_SEEDANCE_20 = VideoModelSpec(
    family="seedance_2_0",
    ratios=frozenset({"16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "adaptive"}),
    resolutions=frozenset({"480p", "720p", "1080p", "4k"}),
    default_resolution="720p",
    max_images=9,
    max_videos=3,
    max_audios=3,
    duration_min=4,
    duration_max=15,
    duration_default=5,
)
_SEEDANCE_20_FAST = VideoModelSpec(
    family="seedance_2_0",
    ratios=_SEEDANCE_20.ratios,
    resolutions=frozenset({"480p", "720p"}),
    default_resolution="720p",
    max_images=9,
    max_videos=3,
    max_audios=3,
    duration_min=4,
    duration_max=15,
    duration_default=5,
)
_SEEDANCE_25 = VideoModelSpec(
    family="seedance_2_5",
    ratios=frozenset({"16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "adaptive"}),
    resolutions=frozenset({"480p", "720p", "1080p"}),
    default_resolution="720p",
    max_images=30,
    max_videos=10,
    max_audios=10,
    duration_min=4,
    duration_max=30,
    duration_default=5,
    duration_special=(-1,),
    allow_audio_only=True,
)
_MINIMAX_H3 = VideoModelSpec(
    family="minimax_h3",
    ratios=frozenset({"21:9", "16:9", "4:3", "1:1", "3:4", "9:16"}),
    resolutions=frozenset({"2K", "768P"}),
    default_resolution="2K",
    max_images=9,
    max_videos=3,
    max_audios=3,
    duration_min=4,
    duration_max=15,
    duration_default=5,
)
_PIXVERSE_V6 = VideoModelSpec(
    family="pixverse_v6",
    ratios=frozenset({"16:9", "4:3", "1:1", "3:4", "9:16", "2:3", "3:2", "21:9"}),
    resolutions=frozenset({"360p", "540p", "720p", "1080p"}),
    default_resolution="540p",
    max_images=7,
    max_videos=0,
    max_audios=0,
    duration_min=1,
    duration_max=15,
    duration_default=5,
    duration_mode_values=(("first_last_frame", (5, 8)),),
)
_GEMINI_OMNI = VideoModelSpec(
    family="gemini_omni_flash_preview",
    ratios=frozenset({"16:9", "9:16"}),
    resolutions=frozenset({"720p"}),
    default_resolution="720p",
    max_images=16,
    max_videos=1,
    max_audios=0,
    duration_min=3,
    duration_max=10,
    duration_default=None,
    duration_controllable=False,
)

_MODEL_SPECS = {
    "seedance-2.0": _SEEDANCE_20,
    "seedance-2.0-face": _SEEDANCE_20,
    "seedance-2.0-fast": _SEEDANCE_20_FAST,
    "seedance-2.0-fast-face": _SEEDANCE_20_FAST,
    "seedance-2.0-mini": _SEEDANCE_20_FAST,
    "seedance-2-0": _SEEDANCE_20,
    "seedance-2.5": _SEEDANCE_25,
    "minimax-h3": _MINIMAX_H3,
    "pixverse-v6": _PIXVERSE_V6,
    "gemini-omni-flash-preview": _GEMINI_OMNI,
}

_IMAGE_KEYS = ("image_urls", "images", "uploaded_images", "reference_images")
_VIDEO_KEYS = ("video_urls", "videos", "uploaded_videos", "reference_videos")
_AUDIO_KEYS = ("audio_urls", "audios", "uploaded_audios", "reference_audios")
_FIRST_FRAME_KEYS = (
    "first_frame_url",
    "first_frame",
    "first_frame_image",
    "first_image_url",
)
_LAST_FRAME_KEYS = (
    "last_frame_url",
    "last_frame",
    "last_frame_image",
    "last_image_url",
)


def is_apimart_video_model(model: str) -> bool:
    return str(model or "").strip().lower() in _MODEL_SPECS


def build_video_payload(model: str, prompt: str, extra: Dict[str, Any]) -> Dict[str, Any]:
    model_id = str(model or "").strip()
    spec = _video_model_spec(model_id)
    normalized_prompt = str(prompt or "").strip()
    if spec.family == "seedance_2_0":
        return _build_seedance_payload(model_id, normalized_prompt, extra, spec, version="2.0")
    if spec.family == "seedance_2_5":
        return _build_seedance_payload(model_id, normalized_prompt, extra, spec, version="2.5")
    if spec.family == "minimax_h3":
        return _build_minimax_payload(model_id, normalized_prompt, extra, spec)
    if spec.family == "pixverse_v6":
        return _build_pixverse_payload(model_id, normalized_prompt, extra, spec)
    return _build_gemini_payload(model_id, normalized_prompt, extra, spec)


def normalize_video_resolution(model: str, value: Any) -> str:
    spec = _video_model_spec(model)
    if value in (None, ""):
        return spec.default_resolution
    raw = str(value).strip()
    aliases = {"480": "480p", "540": "540p", "720": "720p", "1080": "1080p"}
    raw = aliases.get(raw.lower(), raw)
    by_lower = {item.lower(): item for item in spec.resolutions}
    normalized = by_lower.get(raw.lower())
    if normalized is None:
        raise AppException(
            "当前 APIMart 视频模型不支持该 resolution 参数", code=40012, status_code=400
        )
    return normalized


def normalize_video_duration(
    model: str,
    value: Any,
    *,
    mode: Optional[str] = None,
) -> Optional[int]:
    spec = _video_model_spec(model)
    if value in (None, ""):
        return None
    if not spec.duration_controllable:
        raise AppException(
            "当前 APIMart 视频模型不支持 duration 参数，视频时长由厂商自动决定",
            code=40012,
            status_code=400,
        )
    normalized = _int_value("duration", value)
    if normalized in spec.duration_special:
        return normalized
    if not spec.duration_min <= normalized <= spec.duration_max:
        raise AppException(
            f"duration 必须在 {spec.duration_min}-{spec.duration_max} 之间",
            code=40012,
            status_code=400,
        )
    mode_values = dict(spec.duration_mode_values).get(str(mode or ""))
    if mode_values and normalized not in mode_values:
        values = " 或 ".join(str(item) for item in mode_values)
        raise AppException(
            f"当前模式 duration 仅支持 {values}",
            code=40012,
            status_code=400,
        )
    return normalized


def video_billing_duration_seconds(model: str, value: Any) -> int:
    spec = _video_model_spec(model)
    if value in (None, ""):
        return spec.duration_default or 5
    normalized = normalize_video_duration(model, value)
    if normalized in spec.duration_special:
        return spec.duration_max
    return normalized or spec.duration_default or 5


def video_model_capabilities(model: str) -> Dict[str, Any]:
    spec = _video_model_spec(model)
    family_fields = {
        "seedance_2_0": [
            "duration",
            "size",
            "resolution",
            "image_urls",
            "image_with_roles",
            "video_urls",
            "audio_urls",
            "generate_audio",
            "return_last_frame",
            "seed",
            "nsfw_check",
            "tools",
        ],
        "seedance_2_5": [
            "duration",
            "size",
            "resolution",
            "image_urls",
            "image_with_roles",
            "video_urls",
            "audio_urls",
            "generate_audio",
            "return_last_frame",
            "seed",
            "nsfw_check",
            "tools",
            "output_format",
            "omni_reference_task_type",
        ],
        "minimax_h3": [
            "duration",
            "aspect_ratio",
            "resolution",
            "image_urls",
            "image_with_roles",
            "video_urls",
            "audio_urls",
            "watermark",
            "webhook",
            "nsfw_check",
        ],
        "pixverse_v6": [
            "duration",
            "size",
            "resolution",
            "image_urls",
            "img_references",
            "first_frame_image",
            "last_frame_image",
            "extend_from_task_id",
            "seed",
            "negative_prompt",
            "audio",
            "watermark",
            "motion_mode",
            "generate_multi_clip_switch",
            "nsfw_check",
        ],
        "gemini_omni_flash_preview": [
            "aspect_ratio",
            "resolution",
            "image_urls",
            "video_urls",
            "extend_from_task_id",
            "nsfw_check",
        ],
    }
    modes = {
        "seedance_2_0": [
            "text_to_video",
            "image_to_video",
            "video_to_video",
            "first_last_frame",
            "multimodal_reference",
        ],
        "seedance_2_5": [
            "text_to_video",
            "image_to_video",
            "video_to_video",
            "first_last_frame",
            "multimodal_reference",
            "audio_video",
            "edit",
            "extend",
        ],
        "minimax_h3": [
            "text_to_video",
            "image_to_video",
            "video_to_video",
            "first_last_frame",
            "multimodal_reference",
        ],
        "pixverse_v6": [
            "text_to_video",
            "image_to_video",
            "first_last_frame",
            "multimodal_reference",
            "extend",
        ],
        "gemini_omni_flash_preview": [
            "text_to_video",
            "image_to_video",
            "video_to_video",
            "multimodal_reference",
            "extend",
        ],
    }
    duration: Dict[str, Any] = {
        "min": spec.duration_min,
        "max": spec.duration_max,
        "controllable": spec.duration_controllable,
    }
    if spec.duration_default is not None:
        duration["default"] = spec.duration_default
    if spec.duration_special:
        duration["special"] = list(spec.duration_special)
    if spec.duration_mode_values:
        duration["mode_values"] = {mode: list(values) for mode, values in spec.duration_mode_values}
    if not spec.duration_controllable:
        duration["provider_managed"] = True
    request_keys = list(family_fields[spec.family])
    if spec.family in {"seedance_2_0", "seedance_2_5"}:
        request_keys.append("private_avatar")
    capabilities: Dict[str, Any] = {
        "provider": "apimart",
        "model_family": spec.family,
        "endpoint": "/v1/videos/generations",
        "task_endpoint": "/v1/tasks/{task_id}",
        "modes": modes[spec.family],
        "request_keys": request_keys,
        "media_limits": {
            "images": spec.max_images,
            "videos": spec.max_videos,
            "audios": spec.max_audios,
        },
        "ratios": sorted(spec.ratios),
        "resolutions": sorted(spec.resolutions),
        "default_resolution": spec.default_resolution,
        "allow_audio_only": spec.allow_audio_only,
        "supports_async_task": True,
    }
    capabilities["duration"] = duration
    if spec.family in {"seedance_2_0", "seedance_2_5"}:
        capabilities["private_avatar"] = {
            "supported": True,
            "max_assets_per_request": 20,
            "asset_type": "Image",
        }
    return capabilities


def merge_video_capabilities(model: str, saved: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    inferred = video_model_capabilities(model)
    if not saved:
        return inferred
    merged = deepcopy(inferred)
    for key, value in saved.items():
        if value is not None:
            merged[key] = value
    for key in (
        "allow_audio_only",
        "default_resolution",
        "duration",
        "endpoint",
        "media_limits",
        "model_family",
        "modes",
        "provider",
        "ratios",
        "request_keys",
        "resolutions",
        "supports_async_task",
        "task_endpoint",
        "private_avatar",
    ):
        if key in inferred:
            merged[key] = inferred[key]
    return merged


def _build_seedance_payload(
    model: str,
    prompt: str,
    extra: Dict[str, Any],
    spec: VideoModelSpec,
    *,
    version: str,
) -> Dict[str, Any]:
    images = _collect_urls(extra, _IMAGE_KEYS)
    videos = _collect_urls(extra, _VIDEO_KEYS)
    audios = _collect_urls(extra, _AUDIO_KEYS)
    roles = _image_roles(extra)
    _validate_count("参考图片", images, spec.max_images)
    _validate_count("带角色图片", roles, spec.max_images)
    if version == "2.5":
        _validate_count("全部参考图片", [*images, *roles], spec.max_images)
    _validate_count("参考视频", videos, spec.max_videos)
    _validate_count("参考音频", audios, spec.max_audios)
    for url in [*images, *videos, *audios, *(item["url"] for item in roles)]:
        _validate_url(url, allow_asset=True)
    if version == "2.0" and images and roles:
        raise AppException(
            "image_urls 与 image_with_roles 不能同时使用", code=40012, status_code=400
        )
    frame_roles = [item for item in roles if item["role"] in {"first_frame", "last_frame"}]
    if version == "2.0" and frame_roles and (videos or audios):
        raise AppException(
            "Seedance 2.0 首尾帧不能与参考视频或音频同时使用", code=40012, status_code=400
        )
    if audios and not (images or videos or roles) and not spec.allow_audio_only:
        raise AppException("参考音频不能单独使用", code=40012, status_code=400)
    if version == "2.5" and not prompt:
        raise AppException("Seedance 2.5 prompt 不能为空", code=40012, status_code=400)
    if version == "2.0" and not prompt and not (images or videos or audios or roles):
        raise AppException("视频 prompt 不能为空", code=40012, status_code=400)
    prompt_limit = 4000 if version == "2.0" and not model.lower().endswith("-mini") else None
    _validate_prompt_length(prompt, prompt_limit)

    payload: Dict[str, Any] = {"model": model}
    payload["nsfw_check"] = True
    if prompt:
        payload["prompt"] = prompt
    _set_optional(payload, "duration", normalize_video_duration(model, _first(extra, "duration")))
    _set_optional(
        payload, "size", _enum_value(extra, ("size", "aspect_ratio", "ratio"), spec.ratios, "size")
    )
    _set_optional(payload, "resolution", _resolution_if_given(model, extra))
    if images:
        payload["image_urls"] = images
    if roles:
        payload["image_with_roles"] = roles
    if videos:
        payload["video_urls"] = videos
    if audios:
        payload["audio_urls"] = audios
    for key in ("generate_audio", "return_last_frame", "nsfw_check"):
        _copy_bool(payload, extra, key, aliases=("audio",) if key == "generate_audio" else ())
    if version == "2.5":
        payload["watermark"] = False
    else:
        _copy_bool(payload, extra, "watermark")
    _copy_int(payload, extra, "seed")
    _copy_tools(payload, extra)
    if version == "2.5":
        _copy_enum(payload, extra, "output_format", {"mp4", "mov"})
        task_type = _copy_enum(
            payload, extra, "omni_reference_task_type", {"auto", "reference", "edit", "extend"}
        )
        if task_type in {"edit", "extend"} and not videos:
            raise AppException(
                f"Seedance 2.5 {task_type} 模式需要参考视频", code=40012, status_code=400
            )
        if task_type in {"edit", "extend"}:
            payload["size"] = "adaptive"
        if task_type == "edit":
            payload["duration"] = -1
        if frame_roles and not (videos or audios):
            payload["size"] = "adaptive"
    return payload


def _build_minimax_payload(
    model: str, prompt: str, extra: Dict[str, Any], spec: VideoModelSpec
) -> Dict[str, Any]:
    if not prompt:
        raise AppException("MiniMax-H3 prompt 不能为空", code=40012, status_code=400)
    _validate_prompt_length(prompt, 7000)
    images = _collect_urls(extra, _IMAGE_KEYS)
    videos = _collect_urls(extra, _VIDEO_KEYS)
    audios = _collect_urls(extra, _AUDIO_KEYS)
    roles = _image_roles(extra)
    _validate_count("参考图片", images, spec.max_images)
    _validate_count("参考视频", videos, spec.max_videos)
    _validate_count("参考音频", audios, spec.max_audios)
    for url in [*images, *videos, *audios, *(item["url"] for item in roles)]:
        _validate_url(url)
    _validate_count("带角色图片", roles, spec.max_images)
    _validate_count("全部参考图片", [*images, *roles], spec.max_images)
    frame_roles = [item for item in roles if item["role"] in {"first_frame", "last_frame"}]
    reference_roles = [item for item in roles if item["role"] == "reference_image"]
    if frame_roles and (images or videos or audios or reference_roles):
        raise AppException("MiniMax-H3 首尾帧与多模态参考模式不能混用", code=40012, status_code=400)
    if audios and not (images or videos or reference_roles):
        raise AppException("参考音频不能单独使用", code=40012, status_code=400)
    payload: Dict[str, Any] = {"model": model, "prompt": prompt}
    _set_optional(payload, "duration", normalize_video_duration(model, _first(extra, "duration")))
    _set_optional(
        payload,
        "aspect_ratio",
        _enum_value(extra, ("aspect_ratio", "size", "ratio"), spec.ratios, "aspect_ratio"),
    )
    _set_optional(payload, "resolution", _resolution_if_given(model, extra))
    if roles:
        payload["image_with_roles"] = roles
    if images:
        payload["image_urls"] = images
    if videos:
        payload["video_urls"] = videos
    if audios:
        payload["audio_urls"] = audios
    _copy_bool(payload, extra, "watermark", aliases=("aigc_watermark",))
    _copy_bool(payload, extra, "nsfw_check")
    webhook = _first(extra, "webhook")
    if webhook is not None:
        _validate_url(str(webhook))
        payload["webhook"] = str(webhook).strip()
    return payload


def _build_pixverse_payload(
    model: str, prompt: str, extra: Dict[str, Any], spec: VideoModelSpec
) -> Dict[str, Any]:
    if not prompt:
        raise AppException("Pixverse V6 prompt 不能为空", code=40012, status_code=400)
    _validate_prompt_length(prompt, 5000)
    first = _frame_url(extra, "first_frame")
    last = _frame_url(extra, "last_frame")
    role_references = [
        item["url"] for item in _image_roles(extra) if item["role"] == "reference_image"
    ]
    explicit_refs = _collect_urls(extra, ("img_references",))
    images = _collect_urls(extra, _IMAGE_KEYS)
    videos = _collect_urls(extra, _VIDEO_KEYS)
    audios = _collect_urls(extra, _AUDIO_KEYS)
    if videos or audios:
        raise AppException("Pixverse V6 不支持参考视频或参考音频", code=40012, status_code=400)
    if explicit_refs and (images or role_references):
        raise AppException("img_references 不能与其他参考图字段混用", code=40012, status_code=400)
    references = explicit_refs or _dedupe([*images, *role_references])
    extend_task = str(_first(extra, "extend_from_task_id") or "").strip()
    _validate_count("参考图片", references, spec.max_images)
    for url in [*references, *([first] if first else []), *([last] if last else [])]:
        _validate_url(url)
    if last and not first:
        raise AppException("Pixverse V6 尾帧模式必须同时提供首帧", code=40012, status_code=400)
    if bool(first) != bool(last):
        raise AppException("Pixverse V6 首尾帧必须成对提供", code=40012, status_code=400)
    if (first or last) and references:
        raise AppException("Pixverse V6 首尾帧与参考图模式不能混用", code=40012, status_code=400)
    if extend_task and (first or last or references):
        raise AppException("Pixverse V6 延长任务不能同时传入图片", code=40012, status_code=400)
    payload: Dict[str, Any] = {"model": model, "prompt": prompt}
    duration = normalize_video_duration(
        model,
        _first(extra, "duration"),
        mode="first_last_frame" if first else None,
    )
    _set_optional(payload, "duration", duration)
    _set_optional(
        payload, "size", _enum_value(extra, ("size", "aspect_ratio", "ratio"), spec.ratios, "size")
    )
    _set_optional(payload, "resolution", _resolution_if_given(model, extra))
    if first:
        payload["first_frame_image"] = first
        payload["last_frame_image"] = last
    elif references:
        if explicit_refs or len(references) > 1:
            payload["img_references"] = references
        else:
            payload["image_urls"] = references
    if extend_task:
        payload["extend_from_task_id"] = extend_task
    _copy_int(payload, extra, "seed", lower=0, upper=2147483647)
    negative_prompt = _first(extra, "negative_prompt")
    if negative_prompt is not None:
        value = str(negative_prompt)
        _validate_prompt_length(value, 2048, field="negative_prompt")
        payload["negative_prompt"] = value
    for key in ("audio", "watermark", "generate_multi_clip_switch"):
        _copy_bool(payload, extra, key)
    _copy_bool(payload, extra, "nsfw_check")
    _copy_enum(payload, extra, "motion_mode", {"normal"})
    return payload


def _build_gemini_payload(
    model: str, prompt: str, extra: Dict[str, Any], spec: VideoModelSpec
) -> Dict[str, Any]:
    duration_value = _first(
        extra,
        "duration",
        "seconds",
        "generation_seconds",
        "duration_seconds",
        "video_duration_seconds",
    )
    if duration_value is not None:
        normalize_video_duration(model, duration_value)
    images = _collect_urls(extra, _IMAGE_KEYS)
    videos = _collect_urls(extra, _VIDEO_KEYS)
    audios = _collect_urls(extra, _AUDIO_KEYS)
    roles = _image_roles(extra)
    extend_task = str(_first(extra, "extend_from_task_id") or "").strip()
    if audios:
        raise AppException("Gemini Omni 不支持参考音频", code=40012, status_code=400)
    if any(item["role"] in {"first_frame", "last_frame"} for item in roles):
        raise AppException("Gemini Omni 不支持首尾帧模式", code=40012, status_code=400)
    images = _dedupe(
        [*images, *(item["url"] for item in roles if item["role"] == "reference_image")]
    )
    _validate_count("参考图片", images, spec.max_images)
    _validate_count("参考视频", videos, spec.max_videos)
    for url in images:
        _validate_url(url)
    for url in videos:
        _validate_url(url, allow_video_data=True)
    if not prompt and not images and not videos and not extend_task:
        raise AppException(
            "Gemini Omni 至少需要 prompt、图片或视频之一", code=40012, status_code=400
        )
    if extend_task and videos:
        raise AppException(
            "extend_from_task_id 与 video_urls 不能同时使用", code=40012, status_code=400
        )
    payload: Dict[str, Any] = {"model": model}
    if prompt:
        payload["prompt"] = prompt
    _set_optional(
        payload,
        "aspect_ratio",
        _enum_value(extra, ("aspect_ratio", "size", "ratio"), spec.ratios, "aspect_ratio"),
    )
    _set_optional(payload, "resolution", _resolution_if_given(model, extra))
    if images:
        payload["image_urls"] = images
    if videos:
        payload["video_urls"] = videos
    if extend_task:
        payload["extend_from_task_id"] = extend_task
    _copy_bool(payload, extra, "nsfw_check")
    return payload


def _video_model_spec(model: str) -> VideoModelSpec:
    model_id = str(model or "").strip().lower()
    spec = _MODEL_SPECS.get(model_id)
    if spec is None:
        raise AppException("暂不支持该 APIMart 视频模型", code=40007, status_code=400)
    return spec


def _collect_urls(extra: Dict[str, Any], keys: Sequence[str]) -> List[str]:
    urls: List[str] = []
    for key in keys:
        urls.extend(_urls_from_value(extra.get(key)))
    return _dedupe(urls)


def _urls_from_value(value: Any) -> List[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        result: List[str] = []
        for item in value:
            result.extend(_urls_from_value(item))
        return result
    if isinstance(value, dict):
        for key in ("url", "uri", "image_url", "video_url", "audio_url", "file_url"):
            if value.get(key) not in (None, ""):
                return _urls_from_value(value[key])
    return []


def _image_roles(extra: Dict[str, Any]) -> List[Dict[str, str]]:
    result: List[Dict[str, str]] = []
    raw_roles = extra.get("image_with_roles")
    if raw_roles is not None and not isinstance(raw_roles, list):
        raise AppException("image_with_roles 必须是对象数组", code=40012, status_code=400)
    if isinstance(raw_roles, list):
        for item in raw_roles:
            if not isinstance(item, dict):
                raise AppException("image_with_roles 必须是对象数组", code=40012, status_code=400)
            role = _normalize_image_role(item.get("role"))
            url = str(item.get("url") or item.get("image_url") or "").strip()
            if role not in {"first_frame", "last_frame", "reference_image"} or not url:
                raise AppException(
                    "image_with_roles 的 role 或 url 不正确", code=40012, status_code=400
                )
            result.append({"url": url, "role": role})
    for item in extra.get("media_items") or []:
        if not isinstance(item, dict):
            continue
        role = _normalize_image_role(item.get("role"))
        url = _urls_from_value(item)
        if role in {"first_frame", "last_frame", "reference_image"} and url:
            result.append({"url": url[0], "role": role})
    first = _first(extra, *_FIRST_FRAME_KEYS)
    last = _first(extra, *_LAST_FRAME_KEYS)
    if first:
        result.append({"url": str(first).strip(), "role": "first_frame"})
    if last:
        result.append({"url": str(last).strip(), "role": "last_frame"})
    deduped: List[Dict[str, str]] = []
    seen = set()
    for item in result:
        marker = (item["url"], item["role"])
        if marker not in seen:
            seen.add(marker)
            deduped.append(item)
    return deduped


def _frame_url(extra: Dict[str, Any], role: str) -> str:
    keys = _FIRST_FRAME_KEYS if role == "first_frame" else _LAST_FRAME_KEYS
    direct = _first(extra, *keys)
    if direct:
        return str(direct).strip()
    for item in _image_roles(extra):
        if item["role"] == role:
            return item["url"]
    return ""


def _resolution_if_given(model: str, extra: Dict[str, Any]) -> Optional[str]:
    value = _first(extra, "resolution")
    return None if value is None else normalize_video_resolution(model, value)


def _enum_value(
    extra: Dict[str, Any], keys: Sequence[str], allowed: FrozenSet[str], label: str
) -> Optional[str]:
    value = _first(extra, *keys)
    if value is None:
        return None
    normalized = str(value).strip().lower()
    by_lower = {item.lower(): item for item in allowed}
    if normalized not in by_lower:
        raise AppException(f"{label} 参数不支持", code=40012, status_code=400)
    return by_lower[normalized]


def _copy_bool(
    payload: Dict[str, Any], extra: Dict[str, Any], key: str, aliases: Sequence[str] = ()
) -> Optional[bool]:
    value = _first(extra, key, *aliases)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise AppException(f"{key} 必须是布尔值", code=40012, status_code=400)
    payload[key] = value
    return value


def _copy_int(
    payload: Dict[str, Any],
    extra: Dict[str, Any],
    key: str,
    *,
    lower: Optional[int] = None,
    upper: Optional[int] = None,
) -> Optional[int]:
    value = _first(extra, key)
    if value is None:
        return None
    normalized = _int_value(key, value)
    if lower is not None and normalized < lower or upper is not None and normalized > upper:
        raise AppException(f"{key} 参数超出允许范围", code=40012, status_code=400)
    payload[key] = normalized
    return normalized


def _copy_enum(
    payload: Dict[str, Any], extra: Dict[str, Any], key: str, allowed: set[str]
) -> Optional[str]:
    value = _first(extra, key)
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized not in allowed:
        raise AppException(f"{key} 参数不支持", code=40012, status_code=400)
    payload[key] = normalized
    return normalized


def _copy_tools(payload: Dict[str, Any], extra: Dict[str, Any]) -> None:
    value = _first(extra, "tools")
    if value is None:
        return
    if value != [{"type": "web_search"}]:
        raise AppException('tools 仅支持 [{"type": "web_search"}]', code=40012, status_code=400)
    payload["tools"] = value


def _validate_url(value: str, *, allow_asset: bool = False, allow_video_data: bool = False) -> None:
    normalized = str(value or "").strip()
    lowered = normalized.lower()
    if allow_video_data and lowered.startswith("data:video/"):
        return
    scheme = urlsplit(normalized).scheme.lower()
    allowed = {"http", "https"}
    if allow_asset:
        allowed.add("asset")
    if scheme not in allowed:
        raise AppException("媒体地址协议不受当前模型支持", code=40012, status_code=400)


def _normalize_image_role(value: Any) -> str:
    role = str(value or "").strip()
    return {
        "first": "first_frame",
        "last": "last_frame",
        "reference": "reference_image",
    }.get(role, role)


def _validate_count(label: str, values: Sequence[Any], maximum: int) -> None:
    if maximum == 0 and values:
        raise AppException(f"当前模型不支持{label}", code=40012, status_code=400)
    if len(values) > maximum:
        raise AppException(f"{label}最多支持 {maximum} 个", code=40012, status_code=400)


def _validate_prompt_length(value: str, maximum: Optional[int], *, field: str = "prompt") -> None:
    if maximum is not None and len(value) > maximum:
        raise AppException(f"{field} 不能超过 {maximum} 个字符", code=40012, status_code=400)


def _int_value(key: str, value: Any) -> int:
    if isinstance(value, bool):
        raise AppException(f"{key} 必须是整数", code=40012, status_code=400)
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise AppException(f"{key} 必须是整数", code=40012, status_code=400) from exc
    if str(normalized) != str(value).strip() and not isinstance(value, int):
        raise AppException(f"{key} 必须是整数", code=40012, status_code=400)
    return normalized


def _first(extra: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = extra.get(key)
        if value not in (None, ""):
            return value
    return None


def _set_optional(payload: Dict[str, Any], key: str, value: Any) -> None:
    if value is not None:
        payload[key] = value


def _dedupe(values: List[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for value in values:
        normalized = str(value).strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result
