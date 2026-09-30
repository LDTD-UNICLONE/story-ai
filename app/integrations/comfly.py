import json
import logging
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlsplit

import httpx

from app.core.config import settings
from app.core.exceptions import AppException
from app.core.media_inputs import (
    FIRST_FRAME_URL_KEYS,
    LAST_FRAME_URL_KEYS,
    FIRST_FRAME_ROLES,
    LAST_FRAME_ROLES,
    as_list,
    extract_media_url,
)
from app.core.outbound_url import open_safe_http_response, trusted_oss_hosts
from app.integrations.comfly_dimensions import (
    adapt_image_dimensions, adapt_video_dimensions,
    normalize_ratio,
)
from app.integrations.comfly_video_specs import allowed_video_request_keys, merge_video_capabilities


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

CHAT_MESSAGE_KEYS = {"role", "content", "name", "tool_call_id", "tool_calls"}
CHAT_MESSAGE_ROLES = {"system", "user", "assistant", "tool"}
CHAT_CONTENT_PART_TYPES = {"text", "image_url"}
CHAT_CAPABILITIES = {"chat", "analyze_image", "analyze_video"}
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
    "style",
    "user",
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
    "user",
}

IMAGE_RESPONSE_FORMAT_VALUES = {"url", "b64_json"}
IMAGE_QUALITY_VALUES = {"auto", "high", "medium", "low", "standard", "hd"}
IMAGE_RESOLUTION_QUALITY_VALUES = {"1k", "2k", "3k", "4k"}
IMAGE_STYLE_VALUES = {"vivid", "natural"}
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
    "audio",
    "audio_urls",
    "image",
    "image_url",
    "image_urls",
    "reference_image",
    "reference_image_urls",
    "reference_images",
    "first_frame",
    "first_frame_url",
    "last_frame",
    "last_frame_url",
    "media",
    "media_items",
    "video",
    "video_url",
    "video_urls",
    "reference_video",
    "reference_video_urls",
    "reference_videos",
    "aspect_ratio",
    "ratio",
    "camera_fixed",
}

VIDEO_BOOL_REQUEST_KEYS = {
    "camerafixed",
    "enable_upsample",
    "enhance_prompt",
    "generate_audio",
    "hd",
    "private",
    "prompt_extend",
    "return_last_frame",
    "watermark",
}
VIDEO_INT_REQUEST_KEYS = {"seed"}
VIDEO_INT_MAX_VALUES = {"seed": 2147483647}
VIDEO_URL_REQUEST_KEYS = {"audio_url", "character_url", "notify_hook"}
VIDEO_ARRAY_REQUEST_KEYS = {"character_timestamps"}
VIDEO_STRING_REQUEST_KEYS = {"negative_prompt", "resolution", "size"}
VIDEO_URL_OR_BASE64_PREFIXES = ("http://", "https://", "data:")
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


def _headers(idempotency_key: Optional[str] = None) -> Dict[str, str]:
    headers = {
        "Authorization": f"Bearer {_api_key()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _auth_headers(idempotency_key: Optional[str] = None) -> Dict[str, str]:
    headers = {
        "Authorization": f"Bearer {_api_key()}",
        "Accept": "application/json",
    }
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return headers


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
        _client = httpx.AsyncClient(timeout=_timeout(), limits=_limits(), trust_env=False)
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

    payload = _response_payload(response)
    data = payload.get("data")
    if not isinstance(data, list):
        raise AppException("模型列表响应格式错误", code=50203, status_code=502)
    return data


async def _get_json(path: str) -> Dict[str, Any]:
    try:
        client = await _get_client()
        response = await client.get(_url(path), headers=_headers(), timeout=settings.provider_query_timeout_seconds)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        _raise_model_service_http_error(exc, "任务查询失败")
    except httpx.TimeoutException as exc:
        raise AppException("任务查询超时", code=50206, status_code=502) from exc
    except httpx.HTTPError as exc:
        raise AppException("无法连接模型服务", code=50202, status_code=502) from exc

    return _response_payload(response)


async def _post_json(
    path: str,
    payload: Dict[str, Any],
    params: Optional[Dict[str, Any]] = None,
    idempotency_key: Optional[str] = None,
) -> Dict[str, Any]:
    try:
        client = await _get_client()
        response = await client.post(
            _url(path),
            headers=_headers(idempotency_key),
            json=payload,
            params=params,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        _raise_model_service_http_error(exc, "模型调用失败")
    except httpx.TimeoutException as exc:
        raise AppException("模型调用超时", code=50206, status_code=502) from exc
    except httpx.HTTPError as exc:
        raise AppException("无法连接模型服务", code=50202, status_code=502) from exc

    return _response_payload(response)


async def _post_multipart(
    path: str,
    data: Dict[str, Any],
    files: List[tuple[str, tuple[str, bytes, str]]],
    params: Optional[Dict[str, Any]] = None,
    idempotency_key: Optional[str] = None,
) -> Dict[str, Any]:
    try:
        client = await _get_client()
        response = await client.post(
            _url(path),
            headers=_auth_headers(idempotency_key),
            data=data, files=files,
            params=params,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        _raise_model_service_http_error(exc, "图像编辑调用失败")
    except httpx.TimeoutException as exc:
        raise AppException("图像编辑调用超时", code=50206, status_code=502) from exc
    except httpx.HTTPError as exc:
        raise AppException("无法连接模型服务", code=50202, status_code=502) from exc

    return _response_payload(response)


def _response_payload(response: httpx.Response) -> Dict[str, Any]:
    text = response.text or ""
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        payload = _parse_sse_payload(text) or _plain_text_payload(text)
        if payload:
            return payload
        raise AppException(
            "模型服务响应为空或格式错误",
            code=50203,
            status_code=502,
        ) from exc
    if not isinstance(payload, dict):
        return {"data": payload}
    return payload


def _parse_sse_payload(text: str) -> Dict[str, Any]:
    content_parts: List[str] = []
    last_payload: Dict[str, Any] = {}
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        data = line.removeprefix("data:").strip()
        if not data or data == "[DONE]":
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            last_payload = payload
            chunk = _extract_stream_chunk_content(payload)
            if chunk:
                content_parts.append(chunk)
    if content_parts:
        return {
            **last_payload,
            "choices": [{"message": {"content": "".join(content_parts)}}],
        }
    return last_payload


def _extract_stream_chunk_content(payload: Dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return ""
    choice = choices[0]
    delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    content = delta.get("content") or message.get("content") or ""
    if isinstance(content, list):
        return "".join(
            str(item.get("text") or "") if isinstance(item, dict) else str(item) for item in content
        )
    return str(content) if content else ""


def _plain_text_payload(text: str) -> Dict[str, Any]:
    content = (text or "").strip()
    if not content:
        return {}
    logger.warning("Comfly returned non-JSON response: %s", content[:500])
    return {
        "choices": [{"message": {"content": content}}],
        "output": content,
    }


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
        raise AppException(
            "输入内容未通过模型安全校验，请更换内容后重试", code=40017, status_code=400
        ) from exc
    if status_code == 400:
        if any(
            token in response_text.lower() for token in ("sensitive", "privacy", "real person", "content policy", "敏感")
        ):
            raise AppException(
                "输入内容未通过模型安全校验，请更换内容后重试", code=40017, status_code=400
            ) from exc
        raise AppException(
            "当前模型不支持所选参数组合，请调整参数后重试", code=40016, status_code=400
        ) from exc
    raise AppException(fallback, code=50204, status_code=502) from exc


async def create_chat_completion(
    model: str, prompt: str, extra: Dict[str, Any],
    *,
    idempotency_key: Optional[str] = None,
) -> Dict[str, Any]:
    payload = build_chat_completion_payload(model, prompt, extra)
    return await _post_json("/v1/chat/completions", payload, idempotency_key=idempotency_key)


def validate_chat_completion_request(model: str, prompt: str, extra: Dict[str, Any]) -> None:
    build_chat_completion_payload(model, prompt, extra)


def build_chat_completion_payload(model: str, prompt: str, extra: Dict[str, Any]) -> Dict[str, Any]:
    model_id = str(model or "").strip()
    if not model_id:
        raise AppException("Chat model 不能为空", code=40019, status_code=400)
    payload: Dict[str, Any] = {
        "model": model_id,
        "messages": _normalize_chat_messages(
            extra.get("messages") or _build_chat_messages(prompt, extra)
        ),
        "stream": False,
    }
    _merge_chat_extra(payload, extra)
    logger.info(
        "Comfly chat completion payload prepared: model=%s stream=%s message_count=%s keys=%s",
        model_id,
        payload.get("stream"),
        len(payload["messages"]),
        sorted(payload.keys()),
    )
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
            payload[key] = _normalize_chat_bool(key, value)
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
        if key == "logit_bias":
            payload[key] = _normalize_chat_object_or_null(key, value)
            continue
        if key == "response_format":
            payload[key] = _normalize_chat_response_format(value)
            continue
        if key == "tools":
            payload[key] = _normalize_chat_tools(value)
            continue
        if key == "tool_choice":
            payload[key] = _normalize_chat_tool_choice(value)
            continue
        if key == "user":
            payload[key] = str(value)
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
        unknown_keys = set(item) - CHAT_MESSAGE_KEYS
        if unknown_keys:
            raise AppException("Chat message 包含不支持的字段", code=40019, status_code=400)
        if role == "tool" and item.get("tool_call_id") in (None, ""):
            raise AppException(
                "Chat tool message 必须包含 tool_call_id", code=40019, status_code=400
            )
        if item.get("content") is None and item.get("tool_calls") is not None:
            content = ""
        else:
            content = _normalize_chat_content(item.get("content"))
        message = {"role": role, "content": content}
        if item.get("name") not in (None, ""):
            message["name"] = str(item["name"])
        if item.get("tool_call_id") not in (None, ""):
            message["tool_call_id"] = str(item["tool_call_id"])
        if item.get("tool_calls") is not None:
            message["tool_calls"] = _normalize_chat_tool_calls(item["tool_calls"])
        messages.append(message)
    return messages


def _normalize_chat_content(value: Any) -> Any:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        content: List[Dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                raise AppException(
                    "Chat message content 数组项必须是对象", code=40019, status_code=400
                )
            content_type = str(item.get("type") or "").strip()
            if content_type not in CHAT_CONTENT_PART_TYPES:
                raise AppException("Chat message content 类型不支持", code=40019, status_code=400)
            if content_type == "text":
                text = str(item.get("text") or "")
                if not text:
                    raise AppException("Chat 文本内容不能为空", code=40019, status_code=400)
                content.append({"type": "text", "text": text})
                continue
            media_value = item.get(content_type)
            url = extract_media_url(media_value)
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
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise AppException(f"Chat 参数 {key} 必须是整数", code=40019, status_code=400) from exc
    if key == "n" and normalized < 1:
        raise AppException("Chat 参数 n 必须大于等于 1", code=40019, status_code=400)
    if key == "seen" and normalized < 0:
        raise AppException("Chat 参数 seen 必须大于等于 0", code=40019, status_code=400)
    return normalized


def _normalize_chat_stop(value: Any) -> Any:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    raise AppException("Chat 参数 stop 必须是字符串或字符串数组", code=40019, status_code=400)


def _normalize_chat_bool(key: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    raise AppException(f"Chat 参数 {key} 必须是布尔值", code=40019, status_code=400)


def _normalize_chat_object_or_null(key: str, value: Any) -> Any:
    if value is None or isinstance(value, dict):
        return value
    raise AppException(f"Chat 参数 {key} 必须是对象或 null", code=40019, status_code=400)


def _normalize_chat_response_format(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise AppException("Chat 参数 response_format 必须是对象", code=40019, status_code=400)
    response_type = value.get("type")
    if response_type is not None and str(response_type) not in {
        "text",
        "json_object",
        "json_schema",
    }:
        raise AppException("Chat 参数 response_format.type 不支持", code=40019, status_code=400)
    if response_type == "json_schema" and not isinstance(value.get("json_schema"), dict):
        raise AppException(
            "Chat 参数 response_format.json_schema 必须是对象", code=40019, status_code=400
        )
    return value


def _normalize_chat_tools(value: Any) -> List[Any]:
    if not isinstance(value, list):
        raise AppException("Chat 参数 tools 必须是数组", code=40019, status_code=400)
    for tool in value:
        if isinstance(tool, str):
            continue
        if not isinstance(tool, dict):
            raise AppException(
                "Chat 参数 tools 数组项必须是字符串或对象", code=40019, status_code=400
            )
        tool_type = tool.get("type")
        if tool_type is not None and str(tool_type) != "function":
            raise AppException("Chat 参数 tools.type 仅支持 function", code=40019, status_code=400)
        function = tool.get("function")
        if function is not None:
            if not isinstance(function, dict):
                raise AppException(
                    "Chat 参数 tools.function 必须是对象", code=40019, status_code=400
                )
            if not str(function.get("name") or "").strip():
                raise AppException(
                    "Chat 参数 tools.function.name 不能为空", code=40019, status_code=400
                )
    return value


def _normalize_chat_tool_choice(value: Any) -> Any:
    if isinstance(value, str):
        if value not in {"none", "auto", "required"}:
            raise AppException("Chat 参数 tool_choice 字符串值不支持", code=40019, status_code=400)
        return value
    if isinstance(value, dict):
        return value
    raise AppException("Chat 参数 tool_choice 必须是字符串或对象", code=40019, status_code=400)


def _normalize_chat_tool_calls(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        raise AppException("Chat message tool_calls 必须是数组", code=40019, status_code=400)
    for item in value:
        if not isinstance(item, dict):
            raise AppException(
                "Chat message tool_calls 数组项必须是对象", code=40019, status_code=400
            )
    return value


def _collect_chat_media_items(extra: Dict[str, Any]) -> List[tuple[str, str]]:
    image_values: List[Any] = []
    for key in ("images", "image", "image_url", "image_urls"):
        if key in extra:
            image_values.extend(as_list(extra[key]))

    video_values: List[Any] = []
    for key in ("videos", "video", "video_url", "video_urls"):
        if key in extra:
            video_values.extend(as_list(extra[key]))

    items: List[tuple[str, str]] = []
    for value in image_values:
        url = extract_media_url(value)
        if url:
            items.append(("image", url))
    for value in video_values:
        url = extract_media_url(value)
        if url:
            items.append(("video", url))
    return items


async def create_image_generation(
    model: str, prompt: str, extra: Dict[str, Any],
    *,
    idempotency_key: Optional[str] = None,
) -> Dict[str, Any]:
    if _is_image_edit_request(extra):
        return await create_image_edit(
            model,
            prompt,
            extra,
            idempotency_key=idempotency_key,
        )

    payload, params = build_image_generation_payload(model, prompt, extra)
    return await _post_json(
        "/v1/images/generations",
        payload, params=params or None,
        idempotency_key=idempotency_key,
    )


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
    model_id = _normalize_model_id(model, "绘图 model 不能为空")
    payload: Dict[str, Any] = {"model": model_id, "prompt": _normalize_image_prompt(prompt)}
    params = _build_image_query_params(extra)
    _normalize_generation_images(payload, extra)
    _merge_image_extra(payload, extra, IMAGE_GENERATION_REQUEST_KEYS, IMAGE_GENERATION_HELPER_KEYS)
    adapt_image_dimensions(model_id, payload, extra)
    _normalize_image_payload(payload)
    logger.info(
        "Comfly image generation payload prepared: model=%s async=%s keys=%s params=%s",
        model_id,
        params.get("async"),
        sorted(payload.keys()),
        sorted(params.keys()),
    )
    return payload, params


async def create_image_conversation(
    model: str, prompt: str, extra: Dict[str, Any]
) -> Dict[str, Any]:
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
            values.extend(as_list(extra[key]))

    urls: List[str] = []
    for value in values:
        url = extract_media_url(value)
        if url:
            urls.append(url)
    return urls


async def create_image_edit(
    model: str, prompt: str, extra: Dict[str, Any],
    *,
    idempotency_key: Optional[str] = None,
) -> Dict[str, Any]:
    data, params, image_urls, mask_url = build_image_edit_data(model, prompt, extra)

    files: List[tuple[str, tuple[str, bytes, str]]] = []
    for index, image_url in enumerate(image_urls):
        files.append(("image", await _download_upload_file(image_url, f"image_{index + 1}.png")))

    if mask_url:
        files.append(("mask", await _download_upload_file(mask_url, "mask.png")))

    return await _post_multipart(
        "/v1/images/edits",
        data=data, files=files, params=params or None,
        idempotency_key=idempotency_key,
    )


def build_image_edit_data(
    model: str,
    prompt: str,
    extra: Dict[str, Any],
) -> tuple[Dict[str, Any], Dict[str, Any], List[str], Optional[str]]:
    image_urls = _collect_image_input_urls(extra)
    if not image_urls:
        raise AppException("图像编辑需要传入 image", code=40007, status_code=400)

    model_id = _normalize_model_id(model, "绘图 model 不能为空")
    data: Dict[str, Any] = {"model": model_id, "prompt": _normalize_image_prompt(prompt)}
    params = _build_image_query_params(extra)
    _merge_image_extra(data, extra, IMAGE_EDIT_REQUEST_KEYS, IMAGE_EDIT_HELPER_KEYS)
    adapt_image_dimensions(model_id, data, extra)
    _normalize_image_payload(data)

    mask_url = _collect_mask_input_url(extra)
    return data, params, image_urls, mask_url


async def query_image_generation(task_id: str) -> Dict[str, Any]:
    return await _get_json(f"/v1/images/tasks/{task_id}")


async def create_video_generation(
    model: str, prompt: str, extra: Dict[str, Any],
    *,
    idempotency_key: Optional[str] = None,
) -> Dict[str, Any]:
    payload = build_video_generation_payload(model, prompt, extra)
    return await _post_json(
        "/v2/videos/generations",
        payload,
        idempotency_key=idempotency_key,
    )


def build_video_generation_payload(
    model: str, prompt: str, extra: Dict[str, Any]
) -> Dict[str, Any]:
    model_id = _normalize_model_id(model, "视频 model 不能为空")
    capabilities = merge_video_capabilities(model_id, extra.get("_model_capabilities") or {})
    capabilities["model_id"] = model_id
    allowed_keys = allowed_video_request_keys(model_id, capabilities)
    payload: Dict[str, Any] = {"model": model_id, "prompt": _normalize_video_prompt(prompt)}
    _normalize_video_media(payload, extra, allowed_keys, capabilities)
    _merge_video_extra(payload, model_id, extra, VIDEO_GENERATION_HELPER_KEYS, capabilities)
    mode = str(extra.get("video_mode") or extra.get("capability") or "")
    # A project storyboard is an image reference, not a separate provider feature.
    mode = "image_to_video" if mode in {"storyboard", "reference"} else mode
    modes = capabilities.get("modes") or []
    if mode and mode != "generation" and modes and mode not in modes:
        raise AppException("当前视频模型不支持该生成模式", code=40021, status_code=400)
    if mode in {"image_to_video", "first_last_frame"} and not payload.get("images"):
        if not (mode == "image_to_video" and payload.get("videos")):
            raise AppException("当前模式需要模型支持的参考图片或视频", code=40021, status_code=400)
    return payload


async def query_video_generation(task_id: str) -> Dict[str, Any]:
    return await _get_json(f"/v2/videos/generations/{task_id}")


def _build_image_query_params(extra: Dict[str, Any]) -> Dict[str, Any]:
    params: Dict[str, Any] = {"async": "true"}
    async_value = extra.get("async")
    if async_value is None:
        async_value = extra.get("is_async")
    if async_value is not None and not _normalize_bool_param("async", async_value):
        raise AppException("绘图任务必须使用 async=true 异步处理", code=40020, status_code=400)

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
        urls = _collect_urls(as_list(value))
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
            raise AppException(
                "绘图 response_format 只支持 url 或 b64_json", code=40020, status_code=400
            )
        return text
    if key == "quality":
        text = str(value).strip().lower()
        if not text or text in IMAGE_RESOLUTION_QUALITY_VALUES:
            return None
        if text not in IMAGE_QUALITY_VALUES:
            raise AppException("绘图 quality 参数不支持", code=40020, status_code=400)
        return text
    if key == "style":
        text = str(value).strip().lower()
        if not text:
            return None
        if text not in IMAGE_STYLE_VALUES:
            raise AppException("绘图 style 只支持 vivid 或 natural", code=40020, status_code=400)
        return text
    if key == "user":
        text = str(value).strip()
        return text or None
    return value


def _normalize_model_id(model: str, message: str) -> str:
    text = str(model or "").strip()
    if not text:
        raise AppException(message, code=40020, status_code=400)
    return text


def _normalize_image_prompt(prompt: str) -> str:
    text = str(prompt or "").strip()
    if not text:
        raise AppException("绘图 prompt 不能为空", code=40020, status_code=400)
    return text


def _normalize_video_prompt(prompt: str) -> str:
    text = str(prompt or "").strip()
    if not text:
        raise AppException("视频 prompt 不能为空", code=40021, status_code=400)
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
        raise AppException(
            f"绘图参数 {key} 必须在 {lower}-{upper} 之间", code=40020, status_code=400
        )
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
            values.extend(as_list(extra[key]))
    return _collect_urls(values)


def _collect_mask_input_url(extra: Dict[str, Any]) -> Optional[str]:
    values: List[Any] = []
    for key in ("mask", "mask_url", "mask_urls"):
        if key in extra:
            values.extend(as_list(extra[key]))
    urls = _collect_urls(values)
    return urls[0] if urls else None


def _merge_video_extra(
    payload: Dict[str, Any],
    model: str,
    extra: Dict[str, Any],
    excluded_keys: Set[str],
    capabilities: Dict[str, Any],
) -> None:
    allowed_keys = allowed_video_request_keys(model, capabilities)
    for key, value in extra.items():
        if key in excluded_keys or key not in allowed_keys or value is None:
            continue
        if key == "images" and key in payload:
            continue
        if key == "videos" and key in payload:
            continue
        if key == "audio_url" and key in payload:
            continue
        normalized = _normalize_video_request_value(key, value, capabilities)
        if normalized is None:
            continue
        payload[key] = normalized
    if (
        "camerafixed" in allowed_keys and "camerafixed" not in payload and extra.get("camera_fixed") is not None
    ):
        payload["camerafixed"] = _normalize_video_request_value(
            "camerafixed", extra["camera_fixed"], capabilities
        )
    adapt_video_dimensions(payload, extra, allowed_keys)
    _apply_video_ratio_alias(payload, extra, allowed_keys, capabilities)
    _normalize_video_payload(payload, capabilities)


def _normalize_video_media(
    payload: Dict[str, Any],
    extra: Dict[str, Any],
    allowed_keys: Set[str],
    capabilities: Dict[str, Any],
) -> None:
    if "images" not in allowed_keys and _collect_video_image_urls(extra):
        raise AppException("当前视频模型不支持参考图片", code=40021, status_code=400)
    if "videos" not in allowed_keys and _collect_video_urls(extra):
        raise AppException("当前视频模型不支持参考视频", code=40021, status_code=400)
    if "audio_url" not in allowed_keys and _collect_video_audio_url(extra):
        raise AppException("当前视频模型不支持参考音频", code=40021, status_code=400)
    if "images" in allowed_keys:
        image_urls = _collect_video_image_urls(extra)
        if image_urls:
            _validate_video_media_limit("images", image_urls, capabilities)
            payload["images"] = [_normalize_video_image_url(url) for url in image_urls]

    if "videos" in allowed_keys:
        video_urls = _collect_video_urls(extra)
        if video_urls:
            payload["videos"] = [_normalize_video_url(value, "videos") for value in video_urls]

    if "audio_url" in allowed_keys and "audio_url" not in payload:
        audio_url = _collect_video_audio_url(extra)
        if audio_url:
            payload["audio_url"] = _normalize_video_url(audio_url, "audio_url")


def _collect_video_image_urls(extra: Dict[str, Any]) -> List[str]:
    video_mode = str(extra.get("video_mode") or extra.get("capability") or "").strip()
    if video_mode == "first_last_frame" or any(
        key in extra for key in ("first_frame_url", "first_frame", "last_frame_url", "last_frame")
    ):
        return _collect_first_last_frame_urls(extra)

    values: List[Any] = []
    for key in (
        "images",
        "image", "image_url", "image_urls", "reference_images",
        "reference_image_urls",
    ):
        if key in extra:
            values.extend(as_list(extra[key]))
    return _collect_urls(values)


def _collect_first_last_frame_urls(extra: Dict[str, Any]) -> List[str]:
    urls: List[str] = []
    seen: Set[str] = set()
    for url in (
        *_collect_frame_urls(extra, FIRST_FRAME_URL_KEYS, FIRST_FRAME_ROLES),
        *_collect_frame_urls(extra, LAST_FRAME_URL_KEYS, LAST_FRAME_ROLES),
    ):
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _collect_frame_urls(extra: Dict[str, Any], keys: tuple[str, ...], roles: Set[str]) -> List[str]:
    urls: List[str] = []
    for key in keys:
        url = extract_media_url(extra.get(key))
        if url:
            urls.append(url)
    for item in as_list(extra.get("media_items") or extra.get("media")):
        if not isinstance(item, dict) or _normalize_frame_role(item.get("role")) not in roles:
            continue
        url = extract_media_url(item)
        if url:
            urls.append(url)
    return urls


def _normalize_frame_role(value: Any) -> str:
    return str(value or "").strip().replace("-", "_")


def _collect_video_urls(extra: Dict[str, Any]) -> List[str]:
    values: List[Any] = []
    for key in (
        "videos",
        "video", "video_url", "video_urls", "reference_video", "reference_video_urls",
        "reference_videos",
    ):
        if key in extra:
            values.extend(as_list(extra[key]))
    return _collect_urls(values)


def _collect_video_audio_url(extra: Dict[str, Any]) -> Optional[str]:
    values: List[Any] = []
    for key in ("audio_url", "audio", "audio_urls"):
        if key in extra:
            values.extend(as_list(extra[key]))
    urls = _collect_urls(values)
    return urls[0] if urls else None


def _apply_video_ratio_alias(
    payload: Dict[str, Any],
    extra: Dict[str, Any],
    allowed_keys: Set[str],
    capabilities: Dict[str, Any],
) -> None:
    if "ratio" not in allowed_keys or "ratio" in payload:
        return
    ratio = normalize_ratio(extra.get("ratio") or extra.get("aspect_ratio"))
    if not ratio:
        return
    payload["ratio"] = _normalize_video_choice("ratio", ratio, capabilities, value_key="ratios")


def _normalize_video_payload(payload: Dict[str, Any], capabilities: Dict[str, Any]) -> None:
    for key in list(payload.keys()):
        if key in {"model", "prompt", "images", "videos"}:
            continue
        normalized = _normalize_video_request_value(key, payload[key], capabilities)
        if normalized is None:
            payload.pop(key, None)
            continue
        payload[key] = normalized
    _validate_sora2_payload(payload)


def _normalize_video_request_value(key: str, value: Any, capabilities: Dict[str, Any]) -> Any:
    if isinstance(value, str) and not value.strip():
        return None
    if key == "images":
        urls = [_normalize_video_image_url(item) for item in _collect_urls(as_list(value))]
        _validate_video_media_limit("images", urls, capabilities)
        return urls
    if key == "videos":
        return [_normalize_video_url(item, "videos") for item in _collect_urls(as_list(value))]
    if key == "audio_url":
        return _normalize_video_url(value, key)
    if key in VIDEO_URL_REQUEST_KEYS:
        return _normalize_video_url(value, key)
    if key in VIDEO_BOOL_REQUEST_KEYS:
        return _normalize_strict_bool(key, value)
    if key in VIDEO_INT_REQUEST_KEYS:
        return _normalize_video_int(key, value, 0, VIDEO_INT_MAX_VALUES[key])
    if key == "duration":
        return _normalize_video_duration(value, capabilities)
    if key in {"aspect_ratio", "ratio"}:
        ratio = normalize_ratio(value)
        if not ratio:
            raise AppException(f"视频参数 {key} 不支持", code=40021, status_code=400)
        return _normalize_video_choice(key, ratio, capabilities, value_key="ratios")
    if key == "resolution":
        return _normalize_video_resolution_value(value, capabilities)
    if key in VIDEO_ARRAY_REQUEST_KEYS:
        if isinstance(value, list):
            return value
        text = str(value).strip()
        return text or None
    if key in VIDEO_STRING_REQUEST_KEYS:
        text = str(value).strip()
        return text or None
    return value


def _normalize_video_duration(value: Any, capabilities: Dict[str, Any]) -> Any:
    if isinstance(value, bool):
        raise AppException("视频 duration 必须是整数或枚举字符串", code=40021, status_code=400)
    text = str(value).strip()
    if not text:
        return None
    allowed = capabilities.get("durations")
    if allowed:
        allowed_text = {str(item) for item in allowed}
        if text not in allowed_text:
            raise AppException(
                f"视频 duration 仅支持 {', '.join(sorted(allowed_text))}", code=40021,
                status_code=400,
            )
    try:
        number = int(text)
    except ValueError as exc:
        raise AppException("视频 duration 必须是整数", code=40021, status_code=400) from exc
    if number <= 0:
        raise AppException("视频 duration 必须大于 0", code=40021, status_code=400)
    return text if _is_sora2_model(str(capabilities.get("model_id") or "")) else number


def _normalize_video_resolution_value(value: Any, capabilities: Dict[str, Any]) -> str:
    text = str(value or "").strip()
    if not text:
        raise AppException("视频 resolution 不能为空", code=40021, status_code=400)
    allowed = capabilities.get("resolutions")
    if allowed:
        normalized = {str(item).lower(): str(item) for item in allowed}
        if text.lower() not in normalized:
            raise AppException(
                f"视频 resolution 仅支持 {', '.join(str(item) for item in allowed)}", code=40021,
                status_code=400,
            )
        return normalized[text.lower()]
    return text


def _normalize_video_choice(
    key: str, value: str, capabilities: Dict[str, Any], *, value_key: str
) -> str:
    allowed = capabilities.get(value_key)
    if not allowed:
        return value
    normalized = {str(item).lower(): str(item) for item in allowed}
    lowered = str(value).strip().lower()
    if lowered not in normalized:
        raise AppException(
            f"视频参数 {key} 仅支持 {', '.join(str(item) for item in allowed)}", code=40021,
            status_code=400,
        )
    return normalized[lowered]


def _normalize_video_int(key: str, value: Any, lower: int, upper: int) -> int:
    if isinstance(value, bool):
        raise AppException(f"视频参数 {key} 必须是整数", code=40021, status_code=400)
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise AppException(f"视频参数 {key} 必须是整数", code=40021, status_code=400) from exc
    if number < lower or number > upper:
        raise AppException(
            f"视频参数 {key} 必须在 {lower}-{upper} 之间", code=40021, status_code=400
        )
    return number


def _normalize_strict_bool(key: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    raise AppException(f"视频参数 {key} 必须是布尔值", code=40021, status_code=400)


def _normalize_video_image_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text.startswith(VIDEO_URL_OR_BASE64_PREFIXES):
        raise AppException("视频 images 必须是 URL 或 base64 data URL", code=40021, status_code=400)
    if text.startswith(("http://", "https://")):
        return _normalize_video_url(text, "images")
    return text


def _normalize_video_url(value: Any, key: str) -> str:
    url = extract_media_url(value)
    text = str(url or value or "").strip()
    try:
        parsed = urlsplit(text)
        valid = parsed.scheme in {"http", "https"} and bool(parsed.hostname)
    except ValueError:
        valid = False
    if not valid or any(char.isspace() for char in text):
        raise AppException(f"视频参数 {key} 必须是 HTTP(S) URL", code=40021, status_code=400)
    return text


def _validate_video_media_limit(key: str, values: List[str], capabilities: Dict[str, Any]) -> None:
    media_limits = capabilities.get("media_limits") or {}
    limit = media_limits.get(key)
    if isinstance(limit, int) and limit > 0 and len(values) > limit:
        raise AppException(f"视频 {key} 最多支持 {limit} 个", code=40021, status_code=400)


def _validate_sora2_payload(payload: Dict[str, Any]) -> None:
    model_id = str(payload.get("model") or "")
    if not _is_sora2_model(model_id):
        return
    if payload.get("hd") is True and model_id != "sora-2-pro":
        raise AppException("Sora2 仅 sora-2-pro 支持 hd", code=40021, status_code=400)
    if str(payload.get("duration") or "") == "25" and model_id != "sora-2-pro":
        raise AppException("Sora2 仅 sora-2-pro 支持 25 秒", code=40021, status_code=400)


def _is_sora2_model(model: str) -> bool:
    return str(model or "").strip().lower().replace("_", "-").startswith("sora-2")


def _collect_urls(values: List[Any]) -> List[str]:
    urls: List[str] = []
    for value in values:
        url = extract_media_url(value)
        if url:
            urls.append(url)
    return urls


async def _download_upload_file(url: str, fallback_name: str) -> tuple[str, bytes, str]:
    try:
        client = await _get_client()
        response = await _open_safe_upload_response(client, url)
        try:
            response.raise_for_status()
            max_size = settings.max_upload_size_mb * 1024 * 1024
            try:
                content_length = int(response.headers.get("content-length") or 0)
            except ValueError:
                content_length = 0
            if content_length > max_size:
                raise AppException("待编辑图片超过上传大小限制", code=41300, status_code=413)
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > max_size:
                    raise AppException("待编辑图片超过上传大小限制", code=41300, status_code=413)
            content_type = response.headers.get("content-type") or "application/octet-stream"
        finally:
            await response.aclose()
    except httpx.TimeoutException as exc:
        raise AppException("下载待编辑图片超时", code=50207, status_code=502) from exc
    except httpx.HTTPError as exc:
        raise AppException("下载待编辑图片失败", code=50208, status_code=502) from exc

    filename = url.rstrip("/").split("/")[-1].split("?")[0] or fallback_name
    return filename, bytes(content), content_type


async def _open_safe_upload_response(
    client: httpx.AsyncClient,
    source_url: str,
    max_redirects: int = 5,
) -> httpx.Response:
    return await open_safe_http_response(
        client,
        source_url,
        allowed_hosts=trusted_oss_hosts(),
        max_redirects=max_redirects,
    )
