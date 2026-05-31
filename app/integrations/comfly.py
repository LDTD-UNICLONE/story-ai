import logging
from typing import Any, Dict, List, Optional, Set

import httpx

from app.core.config import settings
from app.core.exceptions import AppException
from app.integrations.comfly_dimensions import adapt_image_dimensions, adapt_video_dimensions
from app.integrations.comfly_video_specs import allowed_video_request_keys


logger = logging.getLogger(__name__)

_client: Optional[httpx.AsyncClient] = None


CHAT_BUILDER_KEYS = {
    "capability",
    "chat_mode",
    "image",
    "image_url",
    "image_urls",
    "images",
    "video",
    "video_url",
    "video_urls",
    "videos",
    "system_prompt",
}

CHAT_REQUEST_KEYS = {
    "messages",
    "temperature",
    "top_p",
    "n",
    "stream",
    "stop",
    "max_tokens",
    "presence_penalty",
    "frequency_penalty",
    "logit_bias",
    "user",
    "response_format",
    "seen",
    "tools",
    "tool_choice",
}

CHAT_MESSAGE_ROLES = {"system", "user", "assistant", "tool"}
CHAT_CONTENT_PART_TYPES = {"text", "image_url"}
CHAT_CAPABILITIES = {"chat", "analyze_image", "analyze_video", "generate_image", "edit_image"}
CHAT_MAX_TOKENS_UPPER_BOUND = 65536


IMAGE_GENERATION_HELPER_KEYS = {
    "async",
    "is_async",
    "webhook",
    "image_mode",
    "capability",
    "image_url",
    "image_urls",
    "images",
    "mask",
    "mask_url",
    "mask_urls",
}

IMAGE_GENERATION_REQUEST_KEYS = {
    "size",
    "aspect_ratio",
    "image",
    "n",
    "quality",
    "response_format",
}


IMAGE_EDIT_HELPER_KEYS = {
    "async",
    "is_async",
    "webhook",
    "image_mode",
    "capability",
    "image",
    "image_url",
    "image_urls",
    "images",
    "mask",
    "mask_url",
    "mask_urls",
}

IMAGE_EDIT_REQUEST_KEYS = {
    "aspect_ratio",
    "image_size",
    "n",
    "quality",
    "response_format",
    "size",
}

IMAGE_RESPONSE_FORMAT_VALUES = {"url", "b64_json"}
IMAGE_QUALITY_VALUES = {"auto", "high", "medium", "low", "standard", "hd"}
IMAGE_RESOLUTION_QUALITY_VALUES = {"1k", "2k", "3k", "4k"}
IMAGE_MAX_N = 10

IMAGE_CHAT_HELPER_KEYS = {
    "async",
    "is_async",
    "webhook",
    "image_mode",
    "capability",
    "image",
    "image_url",
    "image_urls",
    "images",
    "mask",
    "size",
    "aspect_ratio",
}


VIDEO_GENERATION_HELPER_KEYS = {
    "video_mode",
    "capability",
    "_model_capabilities",
}


def _base_url() -> str:
    base_url = settings.comfly_base_url
    if not base_url:
        raise AppException("模型服务地址未配置", code=50020, status_code=500)
    return base_url.rstrip("/")


def _api_key() -> str:
    api_key = settings.comfly_api_key
    if not api_key:
        raise AppException("模型服务密钥未配置", code=50021, status_code=500)
    return api_key


def _url(path: str) -> str:
    base_url = _base_url()
    normalized_path = path if path.startswith("/") else f"/{path}"
    if base_url.endswith("/v1") and normalized_path.startswith("/v1/"):
        normalized_path = normalized_path.removeprefix("/v1")
    if base_url.endswith("/v1") and normalized_path.startswith("/v2/"):
        base_url = base_url.removesuffix("/v1")
    return f"{base_url}{normalized_path}"


def _headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {_api_key()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _auth_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {_api_key()}",
        "Accept": "application/json",
    }


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(settings.comfly_timeout_seconds)


def _limits() -> httpx.Limits:
    return httpx.Limits(
        max_connections=settings.comfly_max_connections,
        max_keepalive_connections=settings.comfly_max_keepalive_connections,
    )


async def init_comfly_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=_timeout(), limits=_limits())
    return _client


async def close_comfly_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


async def _get_client() -> httpx.AsyncClient:
    return await init_comfly_client()


async def list_provider_models() -> List[Dict[str, Any]]:
    try:
        client = await _get_client()
        response = await client.get(_url("/v1/models"), headers=_headers())
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        _raise_model_service_http_error(exc, "获取模型列表失败")
    except httpx.TimeoutException as exc:
        raise AppException("获取模型列表超时", code=50205, status_code=502) from exc
    except httpx.HTTPError as exc:
        raise AppException("无法连接模型服务", code=50202, status_code=502) from exc

    payload = response.json()
    data = payload.get("data")
    if not isinstance(data, list):
        raise AppException("模型列表响应格式错误", code=50203, status_code=502)
    return data


async def _get_json(path: str) -> Dict[str, Any]:
    try:
        client = await _get_client()
        response = await client.get(_url(path), headers=_headers())
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        _raise_model_service_http_error(exc, "任务查询失败")
    except httpx.TimeoutException as exc:
        raise AppException("任务查询超时", code=50206, status_code=502) from exc
    except httpx.HTTPError as exc:
        raise AppException("无法连接模型服务", code=50202, status_code=502) from exc

    return response.json()


async def _post_json(
    path: str,
    payload: Dict[str, Any],
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    try:
        client = await _get_client()
        response = await client.post(_url(path), headers=_headers(), json=payload, params=params)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        _raise_model_service_http_error(exc, "模型调用失败")
    except httpx.TimeoutException as exc:
        raise AppException("模型调用超时", code=50206, status_code=502) from exc
    except httpx.HTTPError as exc:
        raise AppException("无法连接模型服务", code=50202, status_code=502) from exc

    return response.json()


async def _post_multipart(
    path: str,
    data: Dict[str, Any],
    files: List[tuple[str, tuple[str, bytes, str]]],
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    try:
        client = await _get_client()
        response = await client.post(_url(path), headers=_auth_headers(), data=data, files=files, params=params)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        _raise_model_service_http_error(exc, "图像编辑调用失败")
    except httpx.TimeoutException as exc:
        raise AppException("图像编辑调用超时", code=50206, status_code=502) from exc
    except httpx.HTTPError as exc:
        raise AppException("无法连接模型服务", code=50202, status_code=502) from exc

    return response.json()


def _raise_model_service_http_error(exc: httpx.HTTPStatusError, fallback: str) -> None:
    status_code = exc.response.status_code if exc.response is not None else 502
    response_text = exc.response.text if exc.response is not None else ""
    if response_text:
        logger.warning(
            "Comfly request failed: status=%s response=%s",
            status_code,
            response_text[:1000],
        )
    if status_code in {429, 500, 502, 503, 504}:
        raise AppException("模型服务繁忙，请稍后再试", code=50204, status_code=502) from exc
    if status_code in {401, 403}:
        raise AppException("模型服务认证失败，请检查服务配置", code=50231, status_code=502) from exc
    if status_code == 451:
        raise AppException("输入内容未通过模型安全校验，请更换内容后重试", code=40017, status_code=400) from exc
    if status_code == 400:
        if any(token in response_text.lower() for token in ("sensitive", "privacy", "real person", "content policy", "敏感")):
            raise AppException("输入内容未通过模型安全校验，请更换内容后重试", code=40017, status_code=400) from exc
        raise AppException("当前模型不支持所选参数组合，请调整参数后重试", code=40016, status_code=400) from exc
    raise AppException(fallback, code=50204, status_code=502) from exc


async def create_chat_completion(model: str, prompt: str, extra: Dict[str, Any]) -> Dict[str, Any]:
    payload = build_chat_completion_payload(model, prompt, extra)
    return await _post_json("/v1/chat/completions", payload)


def validate_chat_completion_request(model: str, prompt: str, extra: Dict[str, Any]) -> None:
    build_chat_completion_payload(model, prompt, extra)


def build_chat_completion_payload(model: str, prompt: str, extra: Dict[str, Any]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": model,
        "messages": _normalize_chat_messages(extra.get("messages") or _build_chat_messages(prompt, extra)),
        "stream": False,
    }
    _merge_chat_extra(payload, extra)
    return payload


def _build_chat_messages(prompt: str, extra: Dict[str, Any]) -> List[Dict[str, Any]]:
    content = _build_chat_content(prompt, extra)
    messages: List[Dict[str, Any]] = []
    system_prompt = str(extra.get("system_prompt") or "").strip()
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": content})
    return messages


def _build_chat_content(prompt: str, extra: Dict[str, Any]) -> Any:
    capability = str(extra.get("capability") or extra.get("chat_mode") or "chat").strip().lower()
    if capability not in CHAT_CAPABILITIES:
        raise AppException("不支持的 Chat 能力", code=40008, status_code=400)

    media_items = _collect_chat_media_items(extra)
    if capability == "chat" and not media_items:
        return prompt

    has_image = any(media_type == "image" for media_type, _ in media_items)
    has_video = any(media_type == "video" for media_type, _ in media_items)
    if capability == "analyze_image" and not has_image:
        raise AppException("图片分析需要传入图片 URL", code=40009, status_code=400)
    if capability == "analyze_video" and not has_video:
        raise AppException("视频分析需要传入视频 URL", code=40009, status_code=400)
    if capability == "edit_image" and not has_image:
        raise AppException("Chat 图像编辑需要传入图片 URL", code=40009, status_code=400)

    content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
    for _, url in media_items:
        content.append({"type": "image_url", "image_url": {"url": url}})
    return content


def _merge_chat_extra(payload: Dict[str, Any], extra: Dict[str, Any]) -> None:
    for key, value in extra.items():
        if key in CHAT_BUILDER_KEYS or key not in CHAT_REQUEST_KEYS or value is None:
            continue
        if key == "messages":
            payload[key] = _normalize_chat_messages(value)
            continue
        if key == "max_tokens":
            payload[key] = _normalize_chat_max_tokens(value)
            continue
        if key == "stream":
            if value:
                raise AppException("当前后端任务模式不支持 Chat 流式返回", code=40019, status_code=400)
            payload["stream"] = False
            continue
        if key in {"temperature", "top_p", "presence_penalty", "frequency_penalty"}:
            payload[key] = _normalize_chat_float(key, value)
            continue
        if key in {"n", "seen"}:
            payload[key] = _normalize_chat_int(key, value)
            continue
        if key == "stop":
            payload[key] = _normalize_chat_stop(value)
            continue
        payload[key] = value


def _normalize_chat_messages(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise AppException("Chat messages 必须是非空数组", code=40019, status_code=400)

    messages: List[Dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise AppException("Chat messages 每一项必须是对象", code=40019, status_code=400)
        role = str(item.get("role") or "").strip()
        if role not in CHAT_MESSAGE_ROLES:
            raise AppException("Chat message role 不正确", code=40019, status_code=400)
        messages.append({"role": role, "content": _normalize_chat_content(item.get("content"))})
    return messages


def _normalize_chat_content(value: Any) -> Any:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        content: List[Dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                raise AppException("Chat message content 数组项必须是对象", code=40019, status_code=400)
            content_type = str(item.get("type") or "").strip()
            if content_type not in CHAT_CONTENT_PART_TYPES:
                raise AppException("Chat message content 类型不支持", code=40019, status_code=400)
            if content_type == "text":
                content.append({"type": "text", "text": str(item.get("text") or "")})
                continue
            media_value = item.get(content_type)
            url = _extract_media_url(media_value)
            if not url:
                raise AppException("Chat 多模态内容缺少媒体 URL", code=40019, status_code=400)
            content.append({"type": content_type, content_type: {"url": url}})
        return content
    raise AppException("Chat message content 必须是字符串或内容数组", code=40019, status_code=400)


def _normalize_chat_max_tokens(value: Any) -> int:
    try:
        return min(max(1, int(value)), CHAT_MAX_TOKENS_UPPER_BOUND)
    except (TypeError, ValueError):
        return CHAT_MAX_TOKENS_UPPER_BOUND


def _normalize_chat_float(key: str, value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise AppException(f"Chat 参数 {key} 必须是数字", code=40019, status_code=400) from exc


def _normalize_chat_int(key: str, value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise AppException(f"Chat 参数 {key} 必须是整数", code=40019, status_code=400) from exc


def _normalize_chat_stop(value: Any) -> Any:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    raise AppException("Chat 参数 stop 必须是字符串或字符串数组", code=40019, status_code=400)


def _collect_chat_media_items(extra: Dict[str, Any]) -> List[tuple[str, str]]:
    image_values: List[Any] = []
    for key in ("images", "image", "image_url", "image_urls"):
        if key in extra:
            image_values.extend(_as_list(extra[key]))

    video_values: List[Any] = []
    for key in ("videos", "video", "video_url", "video_urls"):
        if key in extra:
            video_values.extend(_as_list(extra[key]))

    items: List[tuple[str, str]] = []
    for value in image_values:
        url = _extract_media_url(value)
        if url:
            items.append(("image", url))
    for value in video_values:
        url = _extract_media_url(value)
        if url:
            items.append(("video", url))
    return items


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _extract_media_url(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("url", "image_url", "video_url", "audio_url", "file_url", "oss_url"):
            nested = value.get(key)
            if isinstance(nested, str):
                return nested
            if isinstance(nested, dict) and isinstance(nested.get("url"), str):
                return nested["url"]
    return None


async def create_image_generation(model: str, prompt: str, extra: Dict[str, Any]) -> Dict[str, Any]:
    if _is_image_edit_request(extra):
        return await create_image_edit(model, prompt, extra)

    payload, params = build_image_generation_payload(model, prompt, extra)
    return await _post_json("/v1/images/generations", payload, params=params or None)


def validate_image_request(model: str, prompt: str, extra: Dict[str, Any]) -> None:
    if _is_image_edit_request(extra):
        build_image_edit_data(model, prompt, extra)
        return
    build_image_generation_payload(model, prompt, extra)


def build_image_generation_payload(
    model: str,
    prompt: str,
    extra: Dict[str, Any],
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    payload: Dict[str, Any] = {"model": model, "prompt": _normalize_image_prompt(prompt)}
    params = _build_image_query_params(extra)
    _normalize_generation_images(payload, extra)
    _merge_image_extra(payload, extra, IMAGE_GENERATION_REQUEST_KEYS, IMAGE_GENERATION_HELPER_KEYS)
    adapt_image_dimensions(model, payload, extra)
    _normalize_image_payload(payload)
    return payload, params


async def create_image_conversation(model: str, prompt: str, extra: Dict[str, Any]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": model,
        "messages": extra.get("messages") or _build_image_chat_messages(prompt, extra),
        "stream": False,
    }
    _merge_image_chat_extra(payload, extra)
    return await _post_json("/v1/chat/completions", payload)


def _build_image_chat_messages(prompt: str, extra: Dict[str, Any]) -> List[Dict[str, Any]]:
    content = _build_image_chat_content(prompt, extra)
    return [{"role": "user", "content": content}]


def _build_image_chat_content(prompt: str, extra: Dict[str, Any]) -> Any:
    image_urls = _collect_image_media_urls(extra)
    if not image_urls:
        return prompt

    content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
    for url in image_urls:
        content.append({"type": "image_url", "image_url": {"url": url}})
    return content


def _merge_image_chat_extra(payload: Dict[str, Any], extra: Dict[str, Any]) -> None:
    for key, value in extra.items():
        if key in IMAGE_CHAT_HELPER_KEYS or key not in CHAT_REQUEST_KEYS or value is None:
            continue
        if key == "stream":
            payload["stream"] = False
            continue
        payload[key] = value


def _collect_image_media_urls(extra: Dict[str, Any]) -> List[str]:
    values: List[Any] = []
    for key in ("images", "image", "image_url", "image_urls"):
        if key in extra:
            values.extend(_as_list(extra[key]))

    urls: List[str] = []
    for value in values:
        url = _extract_media_url(value)
        if url:
            urls.append(url)
    return urls


async def create_image_edit(model: str, prompt: str, extra: Dict[str, Any]) -> Dict[str, Any]:
    data, params, image_urls, mask_url = build_image_edit_data(model, prompt, extra)

    files: List[tuple[str, tuple[str, bytes, str]]] = []
    for index, image_url in enumerate(image_urls):
        files.append(("image", await _download_upload_file(image_url, f"image_{index + 1}.png")))

    if mask_url:
        files.append(("mask", await _download_upload_file(mask_url, "mask.png")))

    return await _post_multipart("/v1/images/edits", data=data, files=files, params=params or None)


def build_image_edit_data(
    model: str,
    prompt: str,
    extra: Dict[str, Any],
) -> tuple[Dict[str, Any], Dict[str, Any], List[str], Optional[str]]:
    image_urls = _collect_image_input_urls(extra)
    if not image_urls:
        raise AppException("图像编辑需要传入 image", code=40007, status_code=400)

    data: Dict[str, Any] = {"model": model, "prompt": _normalize_image_prompt(prompt)}
    params = _build_image_query_params(extra)
    _merge_image_extra(data, extra, IMAGE_EDIT_REQUEST_KEYS, IMAGE_EDIT_HELPER_KEYS)
    adapt_image_dimensions(model, data, extra)
    _normalize_image_payload(data)

    mask_url = _collect_mask_input_url(extra)
    return data, params, image_urls, mask_url


async def query_image_generation(task_id: str) -> Dict[str, Any]:
    return await _get_json(f"/v1/images/tasks/{task_id}")


async def create_video_generation(model: str, prompt: str, extra: Dict[str, Any]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"model": model, "prompt": prompt}
    allowed_keys = allowed_video_request_keys(model, extra.get("_model_capabilities") or {})
    _normalize_video_images(payload, extra, allowed_keys)
    _merge_video_extra(payload, model, extra, VIDEO_GENERATION_HELPER_KEYS)
    return await _post_json("/v2/videos/generations", payload)


async def query_video_generation(task_id: str) -> Dict[str, Any]:
    return await _get_json(f"/v2/videos/generations/{task_id}")


def _build_image_query_params(extra: Dict[str, Any]) -> Dict[str, Any]:
    params: Dict[str, Any] = {}
    async_value = extra.get("async")
    if async_value is None:
        async_value = extra.get("is_async")
    if async_value is not None and _normalize_bool_param("async", async_value):
        params["async"] = "true"

    webhook = extra.get("webhook")
    if webhook is not None:
        webhook_value = str(webhook).strip()
        if not webhook_value:
            raise AppException("绘图 webhook 不能为空", code=40020, status_code=400)
        params["webhook"] = webhook_value
    return params


def _is_image_edit_request(extra: Dict[str, Any]) -> bool:
    image_mode = str(extra.get("image_mode") or extra.get("capability") or "generation")
    return image_mode in {"edit", "edits", "image_edit"} or _has_image_mask(extra)


def _merge_image_extra(
    payload: Dict[str, Any],
    extra: Dict[str, Any],
    allowed_keys: Set[str],
    excluded_keys: Set[str],
) -> None:
    for key, value in extra.items():
        if key in excluded_keys or key not in allowed_keys or value is None:
            continue
        if key == "image" and key in payload:
            continue
        normalized = _normalize_image_request_value(key, value)
        if normalized is None:
            continue
        payload[key] = normalized


def _normalize_image_payload(payload: Dict[str, Any]) -> None:
    for key in list(payload.keys()):
        if key in {"model", "prompt"}:
            continue
        normalized = _normalize_image_request_value(key, payload[key])
        if normalized is None:
            payload.pop(key, None)
            continue
        payload[key] = normalized


def _normalize_image_request_value(key: str, value: Any) -> Any:
    if isinstance(value, str) and not value.strip():
        return None
    if key == "image":
        urls = _collect_urls(_as_list(value))
        if not urls:
            raise AppException("图片生成 image 必须是 URL 或 URL 数组", code=40020, status_code=400)
        return urls
    if key == "n":
        return _normalize_bounded_int("n", value, 1, IMAGE_MAX_N)
    if key in {"size", "aspect_ratio", "image_size"}:
        text = str(value).strip()
        return text or None
    if key == "response_format":
        text = str(value).strip()
        if not text:
            return None
        if text not in IMAGE_RESPONSE_FORMAT_VALUES:
            raise AppException("绘图 response_format 只支持 url 或 b64_json", code=40020, status_code=400)
        return text
    if key == "quality":
        text = str(value).strip().lower()
        if not text or text in IMAGE_RESOLUTION_QUALITY_VALUES:
            return None
        if text not in IMAGE_QUALITY_VALUES:
            raise AppException("绘图 quality 参数不支持", code=40020, status_code=400)
        return text
    return value


def _normalize_image_prompt(prompt: str) -> str:
    text = str(prompt or "").strip()
    if not text:
        raise AppException("绘图 prompt 不能为空", code=40020, status_code=400)
    return text


def _normalize_bool_param(key: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "y", "on"}:
            return True
        if lowered in {"false", "0", "no", "n", "off"}:
            return False
    raise AppException(f"绘图参数 {key} 必须是布尔值", code=40020, status_code=400)


def _normalize_bounded_int(key: str, value: Any, lower: int, upper: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise AppException(f"绘图参数 {key} 必须是整数", code=40020, status_code=400) from exc
    if number < lower or number > upper:
        raise AppException(f"绘图参数 {key} 必须在 {lower}-{upper} 之间", code=40020, status_code=400)
    return number


def _normalize_generation_images(payload: Dict[str, Any], extra: Dict[str, Any]) -> None:
    has_image_key = any(key in extra for key in ("image", "images", "image_url", "image_urls"))
    if not has_image_key:
        return
    image_urls = _collect_image_input_urls(extra)
    if not image_urls:
        raise AppException("图片生成 image 必须是 URL 或 URL 数组", code=40020, status_code=400)
    payload["image"] = image_urls


def _has_image_mask(extra: Dict[str, Any]) -> bool:
    return bool(_collect_mask_input_url(extra))


def _collect_image_input_urls(extra: Dict[str, Any]) -> List[str]:
    values: List[Any] = []
    for key in ("image", "images", "image_url", "image_urls"):
        if key in extra:
            values.extend(_as_list(extra[key]))
    return _collect_urls(values)


def _collect_mask_input_url(extra: Dict[str, Any]) -> Optional[str]:
    values: List[Any] = []
    for key in ("mask", "mask_url", "mask_urls"):
        if key in extra:
            values.extend(_as_list(extra[key]))
    urls = _collect_urls(values)
    return urls[0] if urls else None


def _merge_video_extra(
    payload: Dict[str, Any],
    model: str,
    extra: Dict[str, Any],
    excluded_keys: Set[str],
) -> None:
    allowed_keys = allowed_video_request_keys(model, extra.get("_model_capabilities") or {})
    for key, value in extra.items():
        if key in excluded_keys or key not in allowed_keys or value is None:
            continue
        if key == "images" and key in payload:
            continue
        payload[key] = value
    adapt_video_dimensions(payload, extra, allowed_keys)


def _normalize_video_images(payload: Dict[str, Any], extra: Dict[str, Any], allowed_keys: Set[str]) -> None:
    if "images" not in allowed_keys:
        return

    image_urls = _collect_video_image_urls(extra)
    if image_urls:
        payload["images"] = image_urls


def _collect_video_image_urls(extra: Dict[str, Any]) -> List[str]:
    video_mode = str(extra.get("video_mode") or extra.get("capability") or "").strip()
    if video_mode == "first_last_frame":
        return _collect_first_last_frame_urls(extra)

    values: List[Any] = []
    for key in ("images", "image", "image_url", "image_urls", "reference_images", "reference_image_urls"):
        if key in extra:
            values.extend(_as_list(extra[key]))
    return _collect_urls(values)


def _collect_first_last_frame_urls(extra: Dict[str, Any]) -> List[str]:
    urls: List[str] = []
    seen: Set[str] = set()
    for key in ("first_frame_url", "first_frame", "last_frame_url", "last_frame"):
        url = _extract_media_url(extra.get(key))
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    for item in _as_list(extra.get("media_items") or extra.get("media")):
        if not isinstance(item, dict) or item.get("role") not in {"first_frame", "last_frame"}:
            continue
        url = _extract_media_url(item)
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _collect_urls(values: List[Any]) -> List[str]:
    urls: List[str] = []
    for value in values:
        url = _extract_media_url(value)
        if url:
            urls.append(url)
    return urls


async def _download_upload_file(url: str, fallback_name: str) -> tuple[str, bytes, str]:
    try:
        client = await _get_client()
        response = await client.get(url)
        response.raise_for_status()
    except httpx.TimeoutException as exc:
        raise AppException("下载待编辑图片超时", code=50207, status_code=502) from exc
    except httpx.HTTPError as exc:
        raise AppException("下载待编辑图片失败", code=50208, status_code=502) from exc

    content_type = response.headers.get("content-type") or "application/octet-stream"
    filename = url.rstrip("/").split("/")[-1].split("?")[0] or fallback_name
    return filename, response.content, content_type
