from typing import Any, Dict, List, Optional, Set

import httpx

from app.core.config import settings
from app.core.exceptions import AppException
from app.integrations.comfly_dimensions import adapt_image_dimensions, adapt_video_dimensions
from app.integrations.comfly_video_specs import allowed_video_request_keys


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
    "response_format",
}


IMAGE_EDIT_HELPER_KEYS = {
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
    "n",
    "quality",
    "response_format",
    "size",
}

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
    if status_code == 400:
        if any(token in response_text.lower() for token in ("sensitive", "privacy", "real person", "content policy", "敏感")):
            raise AppException("输入内容未通过模型安全校验，请更换内容后重试", code=40017, status_code=400) from exc
        raise AppException("当前模型不支持所选参数组合，请调整参数后重试", code=40016, status_code=400) from exc
    raise AppException(fallback, code=50204, status_code=502) from exc


def _merge_extra(
    payload: Dict[str, Any],
    extra: Dict[str, Any],
    excluded_keys: set[str],
) -> Dict[str, Any]:
    for key, value in extra.items():
        if key not in excluded_keys and value is not None:
            payload[key] = value
    return payload


async def create_chat_completion(model: str, prompt: str, extra: Dict[str, Any]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": model,
        "messages": extra.get("messages") or _build_chat_messages(prompt, extra),
        "stream": False,
    }
    _merge_chat_extra(payload, extra)
    return await _post_json("/v1/chat/completions", payload)


def _build_chat_messages(prompt: str, extra: Dict[str, Any]) -> List[Dict[str, Any]]:
    content = _build_chat_content(prompt, extra)
    return [{"role": "user", "content": content}]


def _build_chat_content(prompt: str, extra: Dict[str, Any]) -> Any:
    capability = str(extra.get("capability") or extra.get("chat_mode") or "chat")
    media_urls = _collect_chat_media_urls(extra)
    if capability in {"chat", "generate_image"} and not media_urls:
        return prompt

    if capability not in {"analyze_image", "analyze_video", "edit_image"}:
        if not media_urls:
            raise AppException("不支持的 Chat 能力", code=40008, status_code=400)
    if not media_urls:
        raise AppException("当前 Chat 能力需要传入媒体 URL", code=40009, status_code=400)

    content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
    for url in media_urls:
        content.append({"type": "image_url", "image_url": {"url": url}})
    return content


def _merge_chat_extra(payload: Dict[str, Any], extra: Dict[str, Any]) -> None:
    for key, value in extra.items():
        if key in CHAT_BUILDER_KEYS or key not in CHAT_REQUEST_KEYS or value is None:
            continue
        if key == "stream":
            payload["stream"] = False
            continue
        payload[key] = value


def _collect_chat_media_urls(extra: Dict[str, Any]) -> List[str]:
    values: List[Any] = []
    for key in ("images", "image", "image_url", "image_urls", "videos", "video", "video_url", "video_urls"):
        if key in extra:
            values.extend(_as_list(extra[key]))

    urls: List[str] = []
    for value in values:
        url = _extract_media_url(value)
        if url:
            urls.append(url)
    return urls


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
    image_mode = str(extra.get("image_mode") or extra.get("capability") or "generation")
    if image_mode in {"edit", "edits", "image_edit"} or _has_image_mask(extra):
        return await create_image_edit(model, prompt, extra)

    payload: Dict[str, Any] = {"model": model, "prompt": prompt}
    params: Dict[str, Any] = {}
    if extra.get("async") is True or extra.get("is_async") is True:
        params["async"] = "true"
    if extra.get("webhook"):
        params["webhook"] = extra["webhook"]
    _normalize_generation_images(payload, extra)
    _merge_allowed_extra(payload, extra, IMAGE_GENERATION_REQUEST_KEYS, IMAGE_GENERATION_HELPER_KEYS)
    adapt_image_dimensions(model, payload, extra)
    return await _post_json("/v1/images/generations", payload, params=params or None)


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
    image_urls = _collect_image_input_urls(extra)
    if not image_urls:
        raise AppException("图像编辑需要传入 image", code=40007, status_code=400)

    data: Dict[str, Any] = {"model": model, "prompt": prompt}
    _merge_allowed_extra(data, extra, IMAGE_EDIT_REQUEST_KEYS, IMAGE_EDIT_HELPER_KEYS)
    adapt_image_dimensions(model, data, extra)

    files: List[tuple[str, tuple[str, bytes, str]]] = []
    for index, image_url in enumerate(image_urls):
        files.append(("image", await _download_upload_file(image_url, f"image_{index + 1}.png")))

    mask_url = _collect_mask_input_url(extra)
    if mask_url:
        files.append(("mask", await _download_upload_file(mask_url, "mask.png")))

    return await _post_multipart("/v1/images/edits", data=data, files=files)


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


def _merge_allowed_extra(
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
        payload[key] = value


def _normalize_generation_images(payload: Dict[str, Any], extra: Dict[str, Any]) -> None:
    image_urls = _collect_image_input_urls(extra)
    if image_urls and "image" not in payload:
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
