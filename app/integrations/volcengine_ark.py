import inspect
import re
from typing import Any, Dict, List, Optional, Set

from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.exceptions import AppException
from app.integrations.comfly import _as_list, _extract_media_url
from app.integrations.comfly_dimensions import normalize_ratio
from app.integrations.volcengine_ark_video_specs import (
    allowed_video_request_keys,
    is_known_video_resolution,
    normalize_video_resolution,
)


_client: Optional[Any] = None


VIDEO_HELPER_KEYS = {
    "_model_capabilities",
    "aspect_ratio",
    "capability",
    "content",
    "media",
    "media_items",
    "audio",
    "audio_url",
    "audio_urls",
    "audios",
    "image",
    "image_url",
    "image_urls",
    "images",
    "reference_first_frame_url",
    "reference_start_frame_url",
    "start_frame_reference_url",
    "video",
    "video_url",
    "video_urls",
    "videos",
    "video_mode",
    "ratio",
    "resolution",
}

DEFAULT_TEXT_VIDEO_RATIO = "adaptive"
DEFAULT_REFERENCE_VIDEO_RATIO = "adaptive"
DEFAULT_VIDEO_DURATION = 5
DEFAULT_GENERATE_AUDIO = True
DEFAULT_WATERMARK = False
MAX_REFERENCE_IMAGES = 9
MAX_REFERENCE_VIDEOS = 3
MAX_REFERENCE_AUDIOS = 3

MEDIA_KEY_BY_TYPE = {
    "image_url": "image_url",
    "video_url": "video_url",
    "audio_url": "audio_url",
}
DEFAULT_ROLE_BY_TYPE = {
    "image_url": "reference_image",
    "video_url": "reference_video",
    "audio_url": "reference_audio",
}
ALLOWED_ROLES_BY_TYPE = {
    "image_url": {"reference_image", "first_frame", "last_frame"},
    "video_url": {"reference_video"},
    "audio_url": {"reference_audio"},
}

UNSUPPORTED_SEEDANCE_2_REQUEST_KEYS = {
    "camera_fixed",
    "draft",
    "draft_task_id",
    "frames",
    "service_tier",
}
MAX_SEED = 2**32 - 1


async def init_volcengine_ark_client() -> Any:
    global _client
    if _client is None:
        _client = await run_in_threadpool(_create_client)
    return _client


async def close_volcengine_ark_client() -> None:
    global _client
    if _client is None:
        return

    close = getattr(_client, "close", None)
    if callable(close):
        result = close()
        if inspect.isawaitable(result):
            await result
    _client = None


async def create_video_generation(model: str, prompt: str, extra: Dict[str, Any]) -> Dict[str, Any]:
    client = await init_volcengine_ark_client()
    model_id = _normalize_model_id(model)
    payload: Dict[str, Any] = {
        "model": model_id,
        "content": _build_content(prompt, extra),
    }
    _merge_video_extra(payload, extra)

    try:
        result = await run_in_threadpool(_create_video_task, client, payload)
    except Exception as exc:
        _raise_provider_error("火山方舟视频生成调用失败", exc)
    return _normalize_task_payload(_to_dict(result))


async def query_video_generation(task_id: str) -> Dict[str, Any]:
    client = await init_volcengine_ark_client()
    try:
        result = await run_in_threadpool(_get_video_task, client, task_id)
    except Exception as exc:
        _raise_provider_error("火山方舟视频任务查询失败", exc)
    return _normalize_task_payload(_to_dict(result))


def _create_client() -> Any:
    if not settings.volcengine_ark_api_key:
        raise AppException("VOLCENGINE_ARK_API_KEY 未配置", code=50031, status_code=500)

    try:
        from volcenginesdkarkruntime import Ark
    except ImportError as exc:
        raise AppException(
            "火山方舟 SDK 未安装，请执行 pip install 'volcengine-python-sdk[ark]'",
            code=50032,
            status_code=500,
        ) from exc

    kwargs: Dict[str, Any] = {
        "api_key": settings.volcengine_ark_api_key,
        "timeout": settings.volcengine_ark_timeout_seconds,
    }
    base_url = _sdk_base_url()
    if base_url:
        kwargs["base_url"] = base_url
    return Ark(**kwargs)


def _sdk_base_url() -> str:
    base_url = (settings.volcengine_ark_base_url or "").rstrip("/")
    if not base_url:
        return ""
    if base_url.endswith("/api/v3"):
        return base_url
    return f"{base_url}/api/v3"


def _content_tasks_resource(client: Any) -> Any:
    content = getattr(client, "content", None)
    if content is not None:
        generations = getattr(content, "generations", None)
        if generations is not None and hasattr(generations, "tasks"):
            return generations.tasks

    content_generation = getattr(client, "content_generation", None)
    if content_generation is not None and hasattr(content_generation, "tasks"):
        return content_generation.tasks

    raise AppException("火山方舟 SDK 不支持视频生成任务接口", code=50033, status_code=500)


def _create_video_task(client: Any, payload: Dict[str, Any]) -> Any:
    tasks = _content_tasks_resource(client)
    payload = _normalize_video_task_payload(payload)
    try:
        return tasks.create(**payload)
    except TypeError:
        return tasks.create(payload)


def _get_video_task(client: Any, task_id: str) -> Any:
    tasks = _content_tasks_resource(client)
    for method_name in ("get", "retrieve"):
        method = getattr(tasks, method_name, None)
        if not callable(method):
            continue
        try:
            return method(task_id=task_id)
        except TypeError:
            return method(task_id)
    raise AppException("火山方舟 SDK 不支持视频任务查询接口", code=50034, status_code=500)


def _build_content(prompt: str, extra: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw_content = extra.get("content")
    if raw_content:
        content = _normalize_content_items(raw_content)
        _validate_multimodal_content(content)
        return content

    content: List[Dict[str, Any]] = []
    text = str(prompt or "").strip()
    if text:
        content.append({"type": "text", "text": text})
    content.extend(_build_media_content(extra))
    _validate_multimodal_content(content)
    return content


def _build_media_content(extra: Dict[str, Any]) -> List[Dict[str, Any]]:
    content: List[Dict[str, Any]] = []
    for url in _collect_image_urls(extra):
        content.append({"type": "image_url", "image_url": {"url": url}, "role": "reference_image"})
    for url in _collect_video_urls(extra):
        content.append({"type": "video_url", "video_url": {"url": url}, "role": "reference_video"})
    for url in _collect_audio_urls(extra):
        content.append({"type": "audio_url", "audio_url": {"url": url}, "role": "reference_audio"})
    for item in _collect_media_items(extra):
        content.append(item)
    return content


def _normalize_content_items(raw_content: Any) -> List[Dict[str, Any]]:
    content: List[Dict[str, Any]] = []
    for value in _as_list(raw_content):
        if not isinstance(value, dict):
            raise AppException("火山方舟 content 数组项必须是对象", code=40010, status_code=400)
        item_type = str(value.get("type") or "").strip()
        if item_type == "text":
            text = str(value.get("text") or "").strip()
            if not text:
                raise AppException("火山方舟 text content 不能为空", code=40010, status_code=400)
            content.append({"type": "text", "text": text})
            continue
        item = _normalize_media_item(value)
        content.append(item)
    return content


def _collect_image_urls(extra: Dict[str, Any]) -> List[str]:
    values: List[Any] = []
    for key in ("images", "image", "image_url", "image_urls"):
        if key in extra:
            values.extend(_as_list(extra[key]))
    return _collect_urls(values)


def _collect_video_urls(extra: Dict[str, Any]) -> List[str]:
    values: List[Any] = []
    for key in ("videos", "video", "video_url", "video_urls"):
        if key in extra:
            values.extend(_as_list(extra[key]))
    return _collect_urls(values)


def _collect_audio_urls(extra: Dict[str, Any]) -> List[str]:
    values: List[Any] = []
    for key in ("audios", "audio", "audio_url", "audio_urls"):
        if key in extra:
            values.extend(_as_list(extra[key]))
    return _collect_urls(values)


def _collect_urls(values: List[Any]) -> List[str]:
    urls: List[str] = []
    seen = set()
    for value in values:
        url = _extract_ark_media_url(value)
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _collect_media_items(extra: Dict[str, Any]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for value in _as_list(extra.get("media") or extra.get("media_items")):
        item = _normalize_media_item(value)
        if item:
            items.append(item)
    return items


def _normalize_media_item(value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(value, dict):
        raise AppException("火山方舟媒体 content 必须是对象", code=40010, status_code=400)

    item_type = str(value.get("type") or "").strip()
    role = value.get("role")
    url = _extract_ark_media_url(value)
    if not item_type:
        item_type = _infer_media_item_type(value)
    if item_type == "draft_task":
        raise AppException("Seedance 2.0 暂不支持 draft_task 输入", code=40010, status_code=400)
    if item_type not in MEDIA_KEY_BY_TYPE:
        raise AppException("火山方舟 content.type 不支持", code=40010, status_code=400)
    if not url:
        raise AppException("火山方舟媒体 content 缺少 url", code=40010, status_code=400)

    media_key = MEDIA_KEY_BY_TYPE[item_type]
    normalized_role = str(role).strip() if role not in (None, "") else ""
    if normalized_role and normalized_role not in ALLOWED_ROLES_BY_TYPE[item_type]:
        raise AppException("火山方舟多模态参考 role 不支持", code=40010, status_code=400)

    normalized: Dict[str, Any] = {"type": item_type, media_key: {"url": url}}
    if normalized_role:
        normalized["role"] = normalized_role
    return normalized


def _normalize_video_task_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(payload)
    normalized["content"] = _normalize_content_items(normalized.get("content") or [])
    _validate_multimodal_content(normalized["content"])
    return normalized


def _infer_media_item_type(value: Dict[str, Any]) -> str:
    if value.get("audio_url") or value.get("audio"):
        return "audio_url"
    if value.get("video_url") or value.get("video"):
        return "video_url"
    return "image_url"


def _extract_ark_media_url(value: Any) -> Optional[str]:
    url = _extract_media_url(value)
    if url:
        return url
    if isinstance(value, dict):
        for key in ("image", "video", "audio"):
            nested = value.get(key)
            if isinstance(nested, str):
                return nested
            if isinstance(nested, dict) and isinstance(nested.get("url"), str):
                return nested["url"]
    return None


def _validate_multimodal_content(content: List[Dict[str, Any]]) -> None:
    if not content:
        raise AppException("火山方舟视频生成 content 不能为空", code=40010, status_code=400)

    image_count = _count_content_type(content, "image_url")
    video_count = _count_content_type(content, "video_url")
    audio_count = _count_content_type(content, "audio_url")

    if image_count > MAX_REFERENCE_IMAGES:
        raise AppException("火山方舟多模态参考最多支持 9 张图片", code=40010, status_code=400)
    if video_count > MAX_REFERENCE_VIDEOS:
        raise AppException("火山方舟多模态参考最多支持 3 个视频", code=40010, status_code=400)
    if audio_count > MAX_REFERENCE_AUDIOS:
        raise AppException("火山方舟多模态参考最多支持 3 个音频", code=40010, status_code=400)
    if audio_count and not image_count and not video_count:
        raise AppException("火山方舟多模态参考不支持仅文本加音频或纯音频输入", code=40010, status_code=400)
    if not image_count and not video_count and not audio_count:
        if not any(item.get("type") == "text" and str(item.get("text") or "").strip() for item in content):
            raise AppException("文生视频需要传入文本提示词", code=40010, status_code=400)
        return

    image_roles = [str(item.get("role") or "") for item in content if item.get("type") == "image_url"]
    has_first_frame = "first_frame" in image_roles
    has_last_frame = "last_frame" in image_roles
    has_reference_image = "reference_image" in image_roles
    has_roleless_image = any(role == "" for role in image_roles)
    has_reference_video_or_audio = video_count > 0 or audio_count > 0

    if has_last_frame:
        if not has_first_frame:
            raise AppException("首尾帧生成必须同时传入 first_frame 和 last_frame", code=40010, status_code=400)
        if image_count != 2 or has_reference_image or has_roleless_image or has_reference_video_or_audio:
            raise AppException("首尾帧生成只允许 1 张首帧图和 1 张尾帧图", code=40010, status_code=400)
        return

    if has_first_frame:
        if image_count != 1 or has_reference_image or has_reference_video_or_audio:
            raise AppException("首帧生成只允许 1 张首帧图，不可混入参考图、视频或音频", code=40010, status_code=400)
        return

    if has_roleless_image and image_count == 1 and not has_reference_video_or_audio:
        return

    if image_count and (has_roleless_image or not has_reference_image):
        raise AppException("多模态参考图片必须设置 role=reference_image", code=40010, status_code=400)
    if video_count and not _all_content_role(content, "video_url", "reference_video"):
        raise AppException("多模态参考视频必须设置 role=reference_video", code=40010, status_code=400)
    if audio_count and not _all_content_role(content, "audio_url", "reference_audio"):
        raise AppException("多模态参考音频必须设置 role=reference_audio", code=40010, status_code=400)


def _count_content_type(content: List[Dict[str, Any]], item_type: str) -> int:
    return sum(1 for item in content if item.get("type") == item_type)


def _all_content_role(content: List[Dict[str, Any]], item_type: str, role: str) -> bool:
    return all(str(item.get("role") or "") == role for item in content if item.get("type") == item_type)


def _merge_video_extra(payload: Dict[str, Any], extra: Dict[str, Any]) -> None:
    capabilities = extra.get("_model_capabilities") or {}
    allowed_keys = allowed_video_request_keys(capabilities)
    _reject_unsupported_seedance_2_keys(extra)
    for key, value in extra.items():
        if key in VIDEO_HELPER_KEYS or key not in allowed_keys or value is None:
            continue
        payload[key] = _normalize_video_request_value(key, value)

    allowed_ratios = set(capabilities.get("ratios") or [])
    ratio = normalize_ratio(extra.get("aspect_ratio") or extra.get("ratio"))
    if ratio and allowed_ratios and ratio not in allowed_ratios:
        raise AppException("火山方舟视频 ratio 参数不支持", code=40010, status_code=400)
    if ratio and "ratio" in allowed_keys and "ratio" not in payload:
        payload["ratio"] = ratio
    if "resolution" in allowed_keys:
        if extra.get("resolution") not in (None, "") and not is_known_video_resolution(extra.get("resolution")):
            raise AppException("火山方舟视频 resolution 参数不支持", code=40010, status_code=400)
        payload["resolution"] = normalize_video_resolution(extra.get("resolution"), capabilities)
    _apply_video_defaults(payload, extra, allowed_keys, capabilities)


def _apply_video_defaults(
    payload: Dict[str, Any],
    extra: Dict[str, Any],
    allowed_keys: Set[str],
    capabilities: Dict[str, Any],
) -> None:
    has_reference_media = _has_reference_media(payload, extra)
    defaults = capabilities.get("defaults") or {}

    if "ratio" in allowed_keys and "ratio" not in payload:
        payload["ratio"] = defaults.get(
            "reference_ratio" if has_reference_media else "text_ratio",
            DEFAULT_REFERENCE_VIDEO_RATIO if has_reference_media else DEFAULT_TEXT_VIDEO_RATIO,
        )
    if "duration" in allowed_keys and "duration" not in payload:
        payload["duration"] = DEFAULT_VIDEO_DURATION
    if "resolution" in allowed_keys and "resolution" not in payload:
        payload["resolution"] = normalize_video_resolution(None, capabilities)
    if "generate_audio" in allowed_keys and "generate_audio" not in payload:
        payload["generate_audio"] = DEFAULT_GENERATE_AUDIO
    if "watermark" not in payload:
        payload["watermark"] = DEFAULT_WATERMARK


def _reject_unsupported_seedance_2_keys(extra: Dict[str, Any]) -> None:
    for key in UNSUPPORTED_SEEDANCE_2_REQUEST_KEYS:
        if key in extra and extra.get(key) is not None:
            raise AppException(f"Seedance 2.0 暂不支持参数 {key}", code=40010, status_code=400)


def _normalize_video_request_value(key: str, value: Any) -> Any:
    if key == "duration":
        return _normalize_duration(value)
    if key in {"generate_audio", "return_last_frame", "watermark"}:
        return _normalize_bool(key, value)
    if key == "resolution":
        return str(value).strip().lower()
    if key == "ratio":
        ratio = normalize_ratio(value)
        if not ratio:
            raise AppException("火山方舟视频 ratio 参数不支持", code=40010, status_code=400)
        return ratio
    if key == "seed":
        return _normalize_int_range("seed", value, -1, MAX_SEED)
    if key == "execution_expires_after":
        return _normalize_int_range("execution_expires_after", value, 3600, 259200)
    if key == "priority":
        return _normalize_int_range("priority", value, 0, 9)
    if key == "callback_url":
        return _normalize_callback_url(value)
    if key == "safety_identifier":
        return _normalize_safety_identifier(value)
    if key == "tools":
        return _normalize_tools(value)
    return value


def _normalize_duration(value: Any) -> int:
    if isinstance(value, bool):
        raise AppException("火山方舟视频 duration 必须是整数", code=40010, status_code=400)
    try:
        duration = int(value)
    except (TypeError, ValueError) as exc:
        raise AppException("火山方舟视频 duration 必须是整数", code=40010, status_code=400) from exc
    if duration == -1:
        return duration
    if duration < 4 or duration > 15:
        raise AppException("Seedance 2.0 视频 duration 仅支持 4-15 秒或 -1", code=40010, status_code=400)
    return duration


def _normalize_bool(key: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    raise AppException(f"火山方舟视频 {key} 必须是布尔值", code=40010, status_code=400)


def _normalize_int_range(key: str, value: Any, lower: int, upper: int) -> int:
    if isinstance(value, bool):
        raise AppException(f"火山方舟视频 {key} 必须是整数", code=40010, status_code=400)
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise AppException(f"火山方舟视频 {key} 必须是整数", code=40010, status_code=400) from exc
    if number < lower or number > upper:
        raise AppException(f"火山方舟视频 {key} 必须在 {lower}-{upper} 之间", code=40010, status_code=400)
    return number


def _normalize_callback_url(value: Any) -> str:
    url = str(value or "").strip()
    if not url.startswith(("http://", "https://")):
        raise AppException("火山方舟视频 callback_url 必须是 http(s) URL", code=40010, status_code=400)
    return url


def _normalize_safety_identifier(value: Any) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 64 or not re.fullmatch(r"[A-Za-z0-9._:-]+", text):
        raise AppException("火山方舟视频 safety_identifier 必须是 1-64 位英文字符串", code=40010, status_code=400)
    return text


def _normalize_tools(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        raise AppException("火山方舟视频 tools 必须是数组", code=40010, status_code=400)
    tools: List[Dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict) or item.get("type") != "web_search":
            raise AppException("火山方舟视频 tools 仅支持 type=web_search", code=40010, status_code=400)
        tools.append({"type": "web_search"})
    return tools


def _normalize_model_id(model: str) -> str:
    model_id = str(model or "").strip()
    if not model_id:
        raise AppException("火山方舟视频 model 不能为空", code=40010, status_code=400)
    return model_id


def _has_reference_media(payload: Dict[str, Any], extra: Dict[str, Any]) -> bool:
    content = payload.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") in {"image_url", "video_url", "audio_url"}:
                return True
    return bool(
        _collect_image_urls(extra)
        or _collect_video_urls(extra)
        or _collect_audio_urls(extra)
        or _collect_media_items(extra)
    )


def _to_dict(value: Any) -> Dict[str, Any]:
    converted = _convert(value)
    if isinstance(converted, dict):
        return converted
    return {"data": converted}


def _normalize_task_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    if payload.get("id") and not payload.get("task_id"):
        payload["task_id"] = payload["id"]
    return payload


def _convert(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _convert(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_convert(item) for item in value]
    if hasattr(value, "model_dump"):
        return _convert(value.model_dump())
    if hasattr(value, "to_dict"):
        return _convert(value.to_dict())
    if hasattr(value, "dict"):
        return _convert(value.dict())
    if hasattr(value, "__dict__") and not isinstance(value, type):
        return {
            key: _convert(item)
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    return value


def _raise_provider_error(prefix: str, exc: Exception) -> None:
    if isinstance(exc, AppException):
        raise exc

    message = str(exc) or exc.__class__.__name__
    status_code = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status_code:
        message = f"HTTP {status_code}: {message}"
    app_status_code = _provider_app_status_code(status_code, message)
    app_code = 40010 if app_status_code < 500 else 50220
    if app_status_code == 400:
        if _is_safety_provider_error(message):
            raise AppException("输入内容未通过模型安全校验，请更换内容后重试", code=40017, status_code=400) from exc
        raise AppException("当前模型不支持所选参数组合，请调整参数后重试", code=40016, status_code=400) from exc
    raise AppException(f"{prefix}：{message}", code=app_code, status_code=app_status_code) from exc


def _provider_app_status_code(status_code: Any, message: str) -> int:
    if _is_non_retryable_provider_error(status_code, message):
        return 400
    return 502


def _is_non_retryable_provider_error(status_code: Any, message: str) -> bool:
    normalized = message.lower()
    if str(status_code) == "400" or "http 400" in normalized:
        return True
    non_retryable_tokens = (
        "badrequest",
        "invalidparameter",
        "sensitivecontentdetected",
        "privacyinformation",
        "real person",
        "not valid",
        "content policy",
    )
    return any(token in normalized for token in non_retryable_tokens)


def _is_safety_provider_error(message: str) -> bool:
    normalized = message.lower()
    return any(
        token in normalized
        for token in ("sensitivecontentdetected", "privacyinformation", "real person", "content policy", "sensitive")
    )
