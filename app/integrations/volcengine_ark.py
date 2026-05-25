import inspect
from typing import Any, Dict, List, Optional, Set

from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.exceptions import AppException
from app.integrations.comfly import _as_list, _extract_media_url
from app.integrations.comfly_dimensions import normalize_ratio
from app.integrations.volcengine_ark_video_specs import allowed_video_request_keys


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
    "video",
    "video_url",
    "video_urls",
    "videos",
    "video_mode",
}

DEFAULT_TEXT_VIDEO_RATIO = "16:9"
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
    payload: Dict[str, Any] = {
        "model": model,
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

    content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
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
            continue
        item_type = str(value.get("type") or "").strip()
        if item_type == "text":
            text = value.get("text")
            if text:
                content.append({"type": "text", "text": str(text)})
            continue
        item = _normalize_media_item(value)
        if item:
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
        return None

    item_type = str(value.get("type") or "").strip()
    role = value.get("role")
    url = _extract_ark_media_url(value)
    if not item_type:
        item_type = _infer_media_item_type(value)
    if item_type not in MEDIA_KEY_BY_TYPE or not url:
        return None

    media_key = MEDIA_KEY_BY_TYPE[item_type]
    normalized_role = str(role) if role else DEFAULT_ROLE_BY_TYPE[item_type]
    if normalized_role not in ALLOWED_ROLES_BY_TYPE[item_type]:
        raise AppException("火山方舟多模态参考 role 不支持", code=40010, status_code=400)

    normalized: Dict[str, Any] = {"type": item_type, media_key: {"url": url}}
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


def _count_content_type(content: List[Dict[str, Any]], item_type: str) -> int:
    return sum(1 for item in content if item.get("type") == item_type)


def _merge_video_extra(payload: Dict[str, Any], extra: Dict[str, Any]) -> None:
    allowed_keys = allowed_video_request_keys(extra.get("_model_capabilities") or {})
    for key, value in extra.items():
        if key in VIDEO_HELPER_KEYS or key not in allowed_keys or value is None:
            continue
        payload[key] = value

    ratio = normalize_ratio(extra.get("aspect_ratio") or extra.get("ratio"))
    if ratio and "ratio" in allowed_keys and "ratio" not in payload:
        payload["ratio"] = ratio
    _apply_video_defaults(payload, extra, allowed_keys)


def _apply_video_defaults(payload: Dict[str, Any], extra: Dict[str, Any], allowed_keys: Set[str]) -> None:
    has_reference_media = _has_reference_media(payload, extra)

    if "ratio" in allowed_keys and "ratio" not in payload:
        payload["ratio"] = DEFAULT_REFERENCE_VIDEO_RATIO if has_reference_media else DEFAULT_TEXT_VIDEO_RATIO
    if "duration" in allowed_keys and "duration" not in payload:
        payload["duration"] = DEFAULT_VIDEO_DURATION
    if "generate_audio" in allowed_keys and "generate_audio" not in payload:
        payload["generate_audio"] = DEFAULT_GENERATE_AUDIO
    if "watermark" not in payload:
        payload["watermark"] = DEFAULT_WATERMARK


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
