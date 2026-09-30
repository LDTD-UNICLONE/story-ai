"""视频对话的模式选择、媒体输入准备与模型规则校验。"""

from typing import Any, Dict, List, Optional, Tuple

from app.core.exceptions import AppException
from app.core.media_inputs import (
    FIRST_FRAME_URL_KEYS,
    LAST_FRAME_URL_KEYS,
    FIRST_FRAME_ROLES,
    LAST_FRAME_ROLES,
    REFERENCE_IMAGE_URL_KEYS,
    REFERENCE_VIDEO_URL_KEYS,
    REFERENCE_AUDIO_URL_KEYS,
    GENERIC_UPLOAD_MEDIA_KEYS,
    as_list,
    drop_frame_url_keys,
    extract_upload_media_type,
    extract_uploaded_media_url,
)
from app.integrations import apimart
from app.integrations.apimart_video_specs import (
    merge_video_capabilities as merge_apimart_video_capabilities,
    normalize_video_resolution as normalize_apimart_video_resolution,
)
from app.integrations.comfly_video_specs import (
    merge_video_capabilities as merge_comfly_video_capabilities,
)
from app.integrations.volcengine_ark_video_specs import (
    is_known_video_resolution,
    is_video_resolution_supported,
    is_volcengine_ark_video_model,
    merge_video_capabilities as merge_ark_video_capabilities,
    normalize_video_resolution,
    validate_reference_video_file,
    validate_reference_video_metadata,
    validate_reference_video_total_duration,
)
from app.models.ai_model import AiModel
from app.services.models.configuration import model_request_capabilities
from app.services.uploads import probe_media_url


CONVERSATION_VIDEO_MODE_TO_PROVIDER_MODE = {
    "text_to_video": "text_to_video",
    "reference": "image_to_video",
    "first_last_frame": "first_last_frame",
}
COMFLY_VIDEO_VENDORS = {"comfly", "模型服务"}


async def build_video_message_extra(
    extra: Dict[str, Any], ai_model: Optional[AiModel] = None
) -> Dict[str, Any]:
    generation_mode = _normalize_conversation_video_generation_mode(
        extra.get("generation_mode"), extra
    )
    payload = dict(extra)
    payload["generation_mode"] = generation_mode
    payload["resolution"] = _normalize_conversation_video_resolution(
        ai_model, payload.get("resolution")
    )
    provider_mode = CONVERSATION_VIDEO_MODE_TO_PROVIDER_MODE[generation_mode]
    payload["video_mode"] = provider_mode
    payload["capability"] = provider_mode

    if generation_mode == "text_to_video":
        if _has_video_media_input(payload):
            raise AppException(
                "文生视频不能传入参考图片、参考视频、参考音频或首尾帧图片",
                code=40012,
                status_code=400,
            )
        _drop_video_media_keys(payload)
        await _validate_conversation_video_model_capability(ai_model, payload, generation_mode)
        return payload

    if generation_mode == "reference":
        reference_images = _dedupe(
            [
                *_collect_reference_image_urls(payload),
                *_collect_media_image_urls(payload, {"reference_image"}, allow_roleless=True),
            ]
        )
        reference_videos = _dedupe(
            [
                *_collect_reference_video_urls(payload),
                *_collect_media_urls(payload, "video_url", {"reference_video"}),
            ]
        )
        reference_audios = _dedupe(
            [
                *_collect_reference_audio_urls(payload),
                *_collect_media_urls(payload, "audio_url", {"reference_audio"}),
            ]
        )
        if reference_images:
            payload["images"] = reference_images
            payload["image_urls"] = reference_images
        if reference_videos:
            payload["videos"] = reference_videos
            payload["video_urls"] = reference_videos
        if reference_audios:
            payload["audios"] = reference_audios
            payload["audio_urls"] = reference_audios
        allow_audio_only = _conversation_video_allows_audio_only(ai_model)
        if reference_audios and not (reference_images or reference_videos) and not allow_audio_only:
            raise AppException(
                "参考音频不能单独使用，需要同时传入参考图片或参考视频", code=40012, status_code=400
            )
        if not reference_images and not reference_videos and not (reference_audios and allow_audio_only):
            raise AppException(
                "参考生成需要至少传入参考图片或参考视频；上传后请把 /uploads/file 返回的 data.url 放入 extra.uploaded_images 或 extra.reference_video_url",
                code=40012,
                status_code=400,
            )
        provider_mode = _reference_video_provider_mode(
            reference_images, reference_videos, reference_audios
        )
        payload["video_mode"] = provider_mode
        payload["capability"] = provider_mode
        await _validate_conversation_video_model_capability(ai_model, payload, generation_mode)
        _drop_reference_media_source_keys(payload)
        drop_frame_url_keys(payload)
        payload.pop("media", None)
        payload.pop("media_items", None)
        payload.pop("content", None)
        return payload

    first_frame_url = _extract_frame_url(payload, FIRST_FRAME_URL_KEYS, FIRST_FRAME_ROLES)
    last_frame_url = _extract_frame_url(payload, LAST_FRAME_URL_KEYS, LAST_FRAME_ROLES)
    if not first_frame_url:
        raise AppException("首尾帧生成需要传入 first_frame_url", code=40012, status_code=400)

    _drop_video_media_keys(payload)
    media_items = []
    payload["first_frame_url"] = first_frame_url
    media_items.append(
        {"type": "image_url", "image_url": {"url": first_frame_url}, "role": "first_frame"}
    )
    if last_frame_url:
        payload["last_frame_url"] = last_frame_url
        media_items.append(
            {"type": "image_url", "image_url": {"url": last_frame_url}, "role": "last_frame"}
        )
    payload["media_items"] = media_items
    await _validate_conversation_video_model_capability(ai_model, payload, generation_mode)
    return payload


def _reference_video_provider_mode(
    reference_images: List[str],
    reference_videos: List[str],
    reference_audios: List[str],
) -> str:
    if reference_videos:
        return "video_to_video"
    if reference_audios:
        return "audio_video"
    return "image_to_video"


async def _validate_conversation_video_model_capability(
    ai_model: Optional[AiModel],
    payload: Dict[str, Any],
    generation_mode: str,
) -> None:
    if ai_model is None:
        return

    capabilities = _conversation_video_capabilities(ai_model)
    if not capabilities:
        return

    modes = _capability_set(capabilities, "modes")
    allowed_keys = _capability_set(capabilities, "request_keys")
    has_input_constraints = bool(modes or allowed_keys or (capabilities.get("media_limits") or {}))
    has_images = _has_value(payload.get("images")) or _has_value(payload.get("image_urls"))
    has_videos = _has_value(payload.get("videos")) or _has_value(payload.get("video_urls"))
    has_audios = _has_value(payload.get("audios")) or _has_value(payload.get("audio_urls"))

    if generation_mode == "text_to_video":
        if modes and "text_to_video" not in modes:
            raise AppException(
                "当前模型不支持文生视频，请切换支持文生视频的模型", code=40012, status_code=400
            )
        return

    if generation_mode == "first_last_frame":
        if modes and "first_last_frame" not in modes:
            raise AppException(
                "当前模型不支持首尾帧生成，请切换支持首尾帧的模型", code=40012, status_code=400
            )
        if not _supports_image_reference(ai_model, capabilities, allowed_keys, modes):
            raise AppException(
                "当前模型不支持首尾帧图片输入，请切换支持图片输入的视频模型",
                code=40012,
                status_code=400,
            )
        return

    if generation_mode != "reference":
        return

    if has_videos and has_input_constraints:
        if not _supports_video_reference(ai_model, capabilities, allowed_keys, modes):
            raise AppException(
                "当前模型不支持参考视频生成，请切换支持参考视频的视频模型，或改用参考图/文生视频",
                code=40012,
                status_code=400,
            )
        if modes and not modes.intersection(
            {"reference", "multimodal_reference", "video_to_video"}
        ):
            raise AppException(
                "当前模型不支持参考视频生成，请切换支持参考视频的视频模型",
                code=40012,
                status_code=400,
            )

    if has_images and has_input_constraints:
        if not _supports_image_reference(ai_model, capabilities, allowed_keys, modes):
            raise AppException(
                "当前模型不支持参考图生成，请切换支持图片输入的视频模型，或改用文生视频",
                code=40012,
                status_code=400,
            )
        if modes and not modes.intersection(
            {"reference", "multimodal_reference", "image_to_video", "video_to_video", "audio_video"}
        ):
            raise AppException(
                "当前模型不支持参考图生成，请切换支持图片输入的视频模型",
                code=40012,
                status_code=400,
            )

    if has_audios and has_input_constraints:
        if not _supports_audio_reference(ai_model, capabilities, allowed_keys, modes):
            raise AppException(
                "当前模型不支持参考音频生成，请切换支持音频输入的视频模型",
                code=40012,
                status_code=400,
            )
        if modes and not modes.intersection({"reference", "multimodal_reference", "audio_video"}):
            raise AppException(
                "当前模型不支持参考音频生成，请切换支持音频输入的视频模型",
                code=40012,
                status_code=400,
            )

    _validate_conversation_video_media_limits(
        capabilities, has_images, has_videos, has_audios, payload
    )
    if has_videos and _is_ark_conversation_video_model(ai_model):
        await _validate_ark_reference_video_metadata(payload)


def _conversation_video_capabilities(ai_model: AiModel) -> Dict[str, Any]:
    saved = model_request_capabilities(ai_model)
    if ai_model.vendor == "volcengine_ark" or is_volcengine_ark_video_model(ai_model.model_id):
        return merge_ark_video_capabilities(ai_model.model_id, saved)
    if ai_model.vendor in COMFLY_VIDEO_VENDORS:
        return merge_comfly_video_capabilities(ai_model.model_id, saved)
    if ai_model.vendor == apimart.APIMART_VENDOR:
        return merge_apimart_video_capabilities(ai_model.model_id, saved)
    return saved


def _conversation_video_allows_audio_only(ai_model: Optional[AiModel]) -> bool:
    if ai_model is None:
        return False
    return bool(_conversation_video_capabilities(ai_model).get("allow_audio_only"))


def _capability_set(capabilities: Dict[str, Any], key: str) -> set[str]:
    return {str(item).strip() for item in (capabilities.get(key) or []) if str(item).strip()}


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(_has_value(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_has_value(item) for item in value)
    return True


def _supports_image_reference(
    ai_model: AiModel,
    capabilities: Dict[str, Any],
    allowed_keys: set[str],
    modes: set[str],
) -> bool:
    limit = _media_limit(capabilities, "images")
    if limit == 0:
        return False
    if allowed_keys and limit < 0:
        return bool(allowed_keys.intersection({"images", "image_urls", "image_with_roles", "first_frame_url"}))
    if _is_ark_conversation_video_model(ai_model):
        return _media_limit(capabilities, "images") != 0
    return (
        "images" in allowed_keys
        or _media_limit(capabilities, "images") > 0
        or bool(
            modes.intersection(
                {
                    "reference",
                    "multimodal_reference", "image_to_video", "video_to_video",
                    "audio_video",
                }
            )
        )
    )


def _supports_video_reference(
    ai_model: AiModel,
    capabilities: Dict[str, Any],
    allowed_keys: set[str],
    modes: set[str],
) -> bool:
    limit = _media_limit(capabilities, "videos")
    if limit == 0:
        return False
    if allowed_keys and limit < 0:
        return bool(allowed_keys.intersection({"videos", "video_urls", "video_url", "reference_video_url"}))
    if _is_ark_conversation_video_model(ai_model):
        return _media_limit(capabilities, "videos") != 0
    return (
        "videos" in allowed_keys
        or _media_limit(capabilities, "videos") > 0
        or bool(modes.intersection({"reference", "multimodal_reference", "video_to_video"}))
    )


def _supports_audio_reference(
    ai_model: AiModel,
    capabilities: Dict[str, Any],
    allowed_keys: set[str],
    modes: set[str],
) -> bool:
    limit = _media_limit(capabilities, "audios", "audio")
    if limit == 0:
        return False
    if allowed_keys and limit < 0:
        return bool(allowed_keys.intersection({"audios", "audio_urls", "audio_url", "reference_audio_url"}))
    if _is_ark_conversation_video_model(ai_model):
        return _media_limit(capabilities, "audios", "audio") != 0
    return (
        "audio_url" in allowed_keys
        or _media_limit(capabilities, "audios", "audio") > 0
        or bool(modes.intersection({"reference", "multimodal_reference", "audio_video"}))
    )


def _is_ark_conversation_video_model(ai_model: AiModel) -> bool:
    return ai_model.vendor == "volcengine_ark" or is_volcengine_ark_video_model(ai_model.model_id)


def _media_limit(capabilities: Dict[str, Any], *keys: str) -> int:
    media_limits = capabilities.get("media_limits") or {}
    for key in keys:
        value = media_limits.get(key)
        if isinstance(value, int):
            return value
    return -1


def _validate_conversation_video_media_limits(
    capabilities: Dict[str, Any],
    has_images: bool,
    has_videos: bool,
    has_audios: bool,
    payload: Dict[str, Any],
) -> None:
    checks = (
        ("images", "参考图", has_images, payload.get("images") or payload.get("image_urls")),
        ("videos", "参考视频", has_videos, payload.get("videos") or payload.get("video_urls")),
        ("audios", "参考音频", has_audios, payload.get("audios") or payload.get("audio_urls")),
    )
    for key, label, enabled, values in checks:
        if not enabled:
            continue
        limit = _media_limit(capabilities, key, key.rstrip("s"))
        if limit > 0 and len(as_list(values)) > limit:
            raise AppException(f"当前模型{label}最多支持 {limit} 个", code=40012, status_code=400)


async def _validate_ark_reference_video_metadata(payload: Dict[str, Any]) -> None:
    reference_urls = _reference_video_url_set(payload)
    uploaded_items = _collect_uploaded_media_items(payload, "video")
    existing_urls = {extract_uploaded_media_url(item) for item in uploaded_items}
    for url in reference_urls:
        if url and url not in existing_urls:
            uploaded_items.append({"url": url})

    if not uploaded_items:
        return

    total_duration = 0.0
    checked_urls: set[str] = set()
    for item in uploaded_items:
        url = extract_uploaded_media_url(item)
        if reference_urls and url not in reference_urls:
            continue
        if url in checked_urls:
            continue
        checked_urls.add(url)
        duration = await _validate_ark_reference_video_item(item)
        if duration is not None:
            total_duration += duration

    validate_reference_video_total_duration(total_duration)


async def _validate_ark_reference_video_item(item: Dict[str, Any]) -> Optional[float]:
    url = extract_uploaded_media_url(item)
    validate_reference_video_file(item)
    media_info = _extract_media_info(item)
    if not media_info and url:
        media_info = await probe_media_url(url, "video") or {}
    if not media_info:
        return None
    return validate_reference_video_metadata(media_info)


def _reference_video_url_set(payload: Dict[str, Any]) -> set[str]:
    urls: set[str] = set()
    for key in ("videos", "video_urls"):
        for value in as_list(payload.get(key)):
            url = extract_uploaded_media_url(value)
            if url:
                urls.add(url)
    return urls


def _drop_reference_media_source_keys(payload: Dict[str, Any]) -> None:
    keep = {"images", "image_urls", "videos", "video_urls", "audios", "audio_urls"}
    for key in (
        *REFERENCE_IMAGE_URL_KEYS,
        *REFERENCE_VIDEO_URL_KEYS,
        *REFERENCE_AUDIO_URL_KEYS,
        *GENERIC_UPLOAD_MEDIA_KEYS,
    ):
        if key not in keep:
            payload.pop(key, None)


def _collect_uploaded_media_items(extra: Dict[str, Any], media_type: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for key in GENERIC_UPLOAD_MEDIA_KEYS:
        for value in as_list(extra.get(key)):
            if not isinstance(value, dict) or _uploaded_media_type(value) != media_type:
                continue
            items.append(value)
    return items


def _extract_media_info(item: Dict[str, Any]) -> Dict[str, Any]:
    for key in ("media_info", "metadata", "meta"):
        value = item.get(key)
        if isinstance(value, dict):
            return value
    for key in ("data", "response", "file", "upload"):
        value = item.get(key)
        if isinstance(value, dict):
            media_info = _extract_media_info(value)
            if media_info:
                return media_info
    return {}


def _normalize_conversation_video_resolution(ai_model: Optional[AiModel], value: Any) -> str:
    if ai_model is not None and ai_model.vendor == apimart.APIMART_VENDOR:
        return normalize_apimart_video_resolution(ai_model.model_id, value)
    if ai_model is not None and (
        ai_model.vendor == "volcengine_ark" or is_volcengine_ark_video_model(ai_model.model_id)
    ):
        if value not in (None, "") and not is_known_video_resolution(value):
            raise AppException("火山方舟视频 resolution 参数不支持", code=40012, status_code=400)
        capabilities = merge_ark_video_capabilities(
            ai_model.model_id,
            model_request_capabilities(ai_model),
        )
        if value not in (None, "") and not is_video_resolution_supported(value, capabilities):
            raise AppException(
                "当前火山方舟视频模型不支持该 resolution 参数", code=40012, status_code=400
            )
        return normalize_video_resolution(value or "720p", capabilities)

    normalized = str(value or "720p").strip().lower()
    aliases = {
        "480": "480p",
        "720": "720p",
        "1080": "1080p",
        "hd": "720p",
        "fhd": "1080p",
    }
    normalized = aliases.get(normalized, normalized)
    return normalized if normalized in {"480p", "720p", "1080p"} else "720p"


def _normalize_conversation_video_generation_mode(
    value: Any, extra: Optional[Dict[str, Any]] = None
) -> str:
    if value in (None, ""):
        extra = extra or {}
        if _has_first_last_frame_input(extra):
            return "first_last_frame"
        if _has_reference_media_input(extra):
            return "reference"
        return "text_to_video"

    mode = str(value).strip()
    aliases = {
        "文生视频": "text_to_video",
        "文本生成视频": "text_to_video",
        "text": "text_to_video",
        "text2video": "text_to_video",
        "textToVideo": "text_to_video",
        "text_to_video": "text_to_video",
        "text-to-video": "text_to_video",
        "t2v": "text_to_video",
        "参考生成": "reference",
        "参考视频生成": "reference",
        "视频参考生成": "reference",
        "referenceGeneration": "reference",
        "reference_generation": "reference",
        "referenceVideo": "reference",
        "reference_video": "reference",
        "videoReference": "reference",
        "video_reference": "reference",
        "video-reference": "reference",
        "videoToVideo": "reference",
        "video_to_video": "reference",
        "video-to-video": "reference",
        "reference-video": "reference",
        "reference-generation": "reference",
        "参考音频生成": "reference",
        "音频参考生成": "reference",
        "referenceAudio": "reference",
        "reference_audio": "reference",
        "audioReference": "reference",
        "audio_reference": "reference",
        "audio-reference": "reference",
        "audioToVideo": "reference",
        "audio_to_video": "reference",
        "audio-to-video": "reference",
        "reference-audio": "reference",
        "imageToVideo": "reference",
        "image_to_video": "reference",
        "multimodalReference": "reference",
        "multimodal_reference": "reference",
        "多模态参考": "reference",
        "多模态参考生成": "reference",
        "参考图生成": "reference",
        "首帧生成": "first_last_frame",
        "首帧模式": "first_last_frame",
        "first_frame": "first_last_frame",
        "first-frame": "first_last_frame",
        "首尾帧生成": "first_last_frame",
        "首尾帧模式": "first_last_frame",
        "firstFrame": "first_last_frame",
        "first-last-frame": "first_last_frame",
        "firstLast": "first_last_frame",
        "firstLastFrame": "first_last_frame",
        "first_last": "first_last_frame",
    }
    mode = aliases.get(mode, mode)
    if mode not in CONVERSATION_VIDEO_MODE_TO_PROVIDER_MODE:
        raise AppException("不支持的视频生成方式", code=40012, status_code=400)
    return mode


def _has_first_last_frame_input(extra: Dict[str, Any]) -> bool:
    return bool(
        _extract_frame_url(extra, FIRST_FRAME_URL_KEYS, FIRST_FRAME_ROLES)
        or _extract_frame_url(extra, LAST_FRAME_URL_KEYS, LAST_FRAME_ROLES)
    )


def _has_reference_media_input(extra: Dict[str, Any]) -> bool:
    return bool(
        _collect_reference_image_urls(extra)
        or _collect_reference_video_urls(extra)
        or _collect_reference_audio_urls(extra)
        or _collect_media_image_urls(extra, {"reference_image"}, allow_roleless=True)
        or _collect_media_urls(extra, "video_url", {"reference_video"})
        or _collect_media_urls(extra, "audio_url", {"reference_audio"})
    )


def _has_video_media_input(extra: Dict[str, Any]) -> bool:
    return _has_first_last_frame_input(extra) or _has_reference_media_input(extra)


def _drop_video_media_keys(extra: Dict[str, Any]) -> None:
    for key in (
        *REFERENCE_IMAGE_URL_KEYS,
        *REFERENCE_VIDEO_URL_KEYS,
        *REFERENCE_AUDIO_URL_KEYS,
        *GENERIC_UPLOAD_MEDIA_KEYS,
        "content", "media", "media_items",
    ):
        extra.pop(key, None)
    drop_frame_url_keys(extra)


def _collect_reference_image_urls(extra: Dict[str, Any]) -> List[str]:
    return [
        *_collect_extra_urls(extra, REFERENCE_IMAGE_URL_KEYS),
        *_collect_uploaded_media_urls(extra, "image"),
    ]


def _collect_reference_video_urls(extra: Dict[str, Any]) -> List[str]:
    return [
        *_collect_extra_urls(extra, REFERENCE_VIDEO_URL_KEYS),
        *_collect_uploaded_media_urls(extra, "video"),
    ]


def _collect_reference_audio_urls(extra: Dict[str, Any]) -> List[str]:
    return [
        *_collect_extra_urls(extra, REFERENCE_AUDIO_URL_KEYS),
        *_collect_uploaded_media_urls(extra, "audio"),
    ]


def _collect_uploaded_media_urls(extra: Dict[str, Any], media_type: str) -> List[str]:
    urls: List[str] = []
    for key in GENERIC_UPLOAD_MEDIA_KEYS:
        for value in as_list(extra.get(key)):
            if _uploaded_media_type(value) != media_type:
                continue
            url = extract_uploaded_media_url(value)
            if url:
                urls.append(url)
    return urls


def _uploaded_media_type(value: Any) -> str:
    raw_type = ""
    if isinstance(value, dict):
        raw_type = extract_upload_media_type(value)
    if raw_type.startswith("image/") or raw_type in {"image", "img", "image_url"}:
        return "image"
    if raw_type.startswith("video/") or raw_type in {"video", "video_url"}:
        return "video"
    if raw_type.startswith("audio/") or raw_type in {"audio", "audio_url"}:
        return "audio"

    url = extract_uploaded_media_url(value).lower().split("?", 1)[0]
    if url.endswith((".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".gif", ".heic", ".heif")):
        return "image"
    if url.endswith((".mp4", ".mov", ".webm", ".m4v", ".avi", ".mkv")):
        return "video"
    if url.endswith((".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg")):
        return "audio"
    return ""


def _collect_extra_urls(extra: Dict[str, Any], keys: Tuple[str, ...]) -> List[str]:
    urls: List[str] = []
    for key in keys:
        for value in as_list(extra.get(key)):
            url = extract_uploaded_media_url(value)
            if url:
                urls.append(url)
    return urls


def _extract_frame_url(extra: Dict[str, Any], keys: Tuple[str, ...], roles: set[str]) -> str:
    for key in keys:
        url = extract_uploaded_media_url(extra.get(key))
        if url:
            return url
    for key in ("media_items", "media", "content"):
        for item in as_list(extra.get(key)):
            if not isinstance(item, dict):
                continue
            role = _normalize_frame_role(item.get("role"))
            if role not in roles:
                continue
            url = extract_uploaded_media_url(item)
            if url:
                return url
    return ""


def _collect_media_image_urls(
    extra: Dict[str, Any], roles: set[str], *, allow_roleless: bool = False
) -> List[str]:
    urls: List[str] = []
    for key in ("media_items", "media", "content"):
        for item in as_list(extra.get(key)):
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "image_url").strip()
            if item_type != "image_url":
                continue
            role = _normalize_frame_role(item.get("role"))
            if role not in roles and not (allow_roleless and not role):
                continue
            url = extract_uploaded_media_url(item)
            if url:
                urls.append(url)
    return urls


def _collect_media_urls(extra: Dict[str, Any], item_type: str, roles: set[str]) -> List[str]:
    urls: List[str] = []
    for key in ("media_items", "media", "content"):
        for item in as_list(extra.get(key)):
            if not isinstance(item, dict):
                continue
            if str(item.get("type") or "").strip() != item_type:
                continue
            role = _normalize_frame_role(item.get("role"))
            if role and role not in roles:
                continue
            url = extract_uploaded_media_url(item)
            if url:
                urls.append(url)
    return urls


def _normalize_frame_role(value: Any) -> str:
    role = str(value or "").strip().replace("-", "_")
    aliases = {
        "reference": "reference_image",
        "referenceImage": "reference_image",
        "ref_image": "reference_image",
        "refImage": "reference_image",
        "image": "reference_image",
        "video": "reference_video",
        "ref_video": "reference_video",
        "refVideo": "reference_video",
        "referenceVideo": "reference_video",
        "audio": "reference_audio",
        "ref_audio": "reference_audio",
        "refAudio": "reference_audio",
        "referenceAudio": "reference_audio",
        "firstFrame": "first_frame",
        "startFrame": "start_frame",
        "referenceFirstFrame": "reference_first_frame",
        "referenceStartFrame": "reference_start_frame",
        "lastFrame": "last_frame",
        "endFrame": "end_frame",
        "endingFrame": "ending_frame",
        "tailFrame": "tail_frame",
        "referenceLastFrame": "reference_last_frame",
        "referenceEndFrame": "reference_end_frame",
    }
    return aliases.get(role, role)


def _dedupe(values: List[str]) -> List[str]:
    items: List[str] = []
    seen = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            items.append(value)
    return items
