import json
from typing import Any, Awaitable, Callable, Dict, List, Optional
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
)

from app.core.config import settings
from app.core.exceptions import AppException
from app.integrations.apimart_image_specs import (
    build_image_payload,
    preserves_reference_image_duplicates,
    requires_stable_response_header,
)
from app.integrations.apimart_video_specs import build_video_payload


APIMART_VENDOR = "apimart"

_client: Optional[AsyncOpenAI] = None

_CHAT_REQUEST_KEYS = {
    "frequency_penalty",
    "logit_bias",
    "max_completion_tokens",
    "max_tokens",
    "n",
    "presence_penalty",
    "response_format",
    "seed",
    "stop",
    "temperature",
    "tool_choice",
    "tools",
    "top_p",
    "user",
}

_RESPONSES_REQUEST_KEYS = {"max_tokens", "temperature", "tools", "top_p"}
_IMAGE_URL_PREFIXES = (
    "http://",
    "https://",
    "data:image/jpeg;base64,",
    "data:image/png;base64,",
    "data:image/gif;base64,",
    "data:image/webp;base64,",
)
_VIDEO_INPUT_KEYS = {
    "video",
    "video_url",
    "video_urls",
    "videos",
    "uploaded_videos",
    "uploadedVideos",
}
_IMAGE_INPUT_KEYS = (
    "image",
    "image_url",
    "image_urls",
    "images",
    "imageUrl",
    "imageUrls",
    "uploaded_images",
    "uploadedImages",
    "reference_image",
    "reference_image_url",
    "reference_images",
    "reference_image_urls",
    "referenceImage",
    "referenceImageUrl",
    "referenceImages",
    "referenceImageUrls",
)

TextDeltaCallback = Callable[[str], Awaitable[None]]
_RESPONSES_FALLBACK_STATUS_CODES = {400, 404, 405, 422}


def _api_key() -> str:
    if not settings.apimart_api_key:
        raise AppException("APIMart 密钥未配置", code=50021, status_code=500)
    return settings.apimart_api_key


def _base_url() -> str:
    if not settings.apimart_base_url:
        raise AppException("APIMart 服务地址未配置", code=50020, status_code=500)
    return settings.apimart_base_url.rstrip("/")


async def init_apimart_client() -> AsyncOpenAI:
    global _client
    if _client is None or _client.is_closed():
        _client = AsyncOpenAI(
            api_key=_api_key(),
            base_url=_base_url(),
            timeout=settings.apimart_timeout_seconds,
            max_retries=0,
            http_client=httpx.AsyncClient(
                limits=httpx.Limits(
                    max_connections=settings.apimart_max_connections,
                    max_keepalive_connections=settings.apimart_max_keepalive_connections,
                ),
                trust_env=False,
            ),
        )
    return _client


async def close_apimart_client() -> None:
    global _client
    if _client is not None and not _client.is_closed():
        await _client.close()
    _client = None


async def _get_client() -> AsyncOpenAI:
    return await init_apimart_client()


async def list_provider_models() -> List[Dict[str, Any]]:
    try:
        response = await (await _get_client()).models.list()
    except Exception as exc:
        _raise_provider_error("获取 APIMart 模型列表失败", exc)
    return [item.model_dump(mode="json") for item in response.data]


def validate_chat_completion_request(model: str, prompt: str, extra: Dict[str, Any]) -> None:
    build_responses_payload(model, prompt, extra)


def build_chat_completion_payload(model: str, prompt: str, extra: Dict[str, Any]) -> Dict[str, Any]:
    validate_chat_completion_request(model, prompt, extra)
    messages = list(extra.get("messages") or [{"role": "user", "content": prompt}])
    system_prompt = str(extra.get("system_prompt") or "").strip()
    if system_prompt:
        messages.insert(0, {"role": "system", "content": system_prompt})
    payload: Dict[str, Any] = {
        "model": model.strip(),
        "messages": messages,
        "stream": True,
    }
    for key in _CHAT_REQUEST_KEYS:
        value = extra.get(key)
        if value is not None:
            payload[key] = value
    return payload


async def create_chat_completion(
    model: str,
    prompt: str,
    extra: Dict[str, Any],
    *,
    stream: bool = False,
    idempotency_key: Optional[str] = None,
    on_text_delta: Optional[TextDeltaCallback] = None,
) -> Dict[str, Any]:
    if not stream:
        return await _create_non_streaming_chat_completion(
            model,
            prompt,
            extra,
            idempotency_key=idempotency_key,
        )
    if not _should_use_responses(extra):
        return await _create_streaming_chat_completion(
            model,
            prompt,
            extra,
            idempotency_key=idempotency_key,
            on_text_delta=on_text_delta,
        )
    try:
        return await create_multimodal_response(
            model,
            prompt,
            extra,
            idempotency_key=idempotency_key,
            on_text_delta=on_text_delta,
        )
    except APIStatusError as exc:
        if not _can_fallback_to_chat(extra, exc):
            _raise_provider_error("APIMart Responses 调用失败", exc)
    result = await _create_streaming_chat_completion(
        model,
        prompt,
        extra,
        idempotency_key=idempotency_key,
        on_text_delta=on_text_delta,
    )
    result["provider_api"] = "chat_completions_fallback"
    return result


def validate_image_request(model: str, prompt: str, extra: Dict[str, Any]) -> None:
    build_image_generation_payload(model, prompt, extra)


def build_image_generation_payload(
    model: str,
    prompt: str,
    extra: Dict[str, Any],
) -> Dict[str, Any]:
    image_urls = _collect_extra_image_urls(
        extra,
        deduplicate=not preserves_reference_image_duplicates(model),
        validate=False,
    )
    return build_image_payload(model, prompt, extra, image_urls)


async def create_image_generation(
    model: str,
    prompt: str,
    extra: Dict[str, Any],
    *,
    idempotency_key: Optional[str] = None,
) -> Dict[str, Any]:
    payload = build_image_generation_payload(model, prompt, extra)
    try:
        response = await (await _get_client()).post(
            "/images/generations",
            cast_to=httpx.Response,
            body=payload,
            options=_image_request_options(model, idempotency_key),
        )
        result = await _read_json_response(response, "APIMart 图像响应格式错误")
    except Exception as exc:
        _raise_provider_error("APIMart 图像生成调用失败", exc)
    return result


def validate_video_request(model: str, prompt: str, extra: Dict[str, Any]) -> None:
    build_video_generation_payload(model, prompt, extra)


def build_video_generation_payload(
    model: str,
    prompt: str,
    extra: Dict[str, Any],
) -> Dict[str, Any]:
    return build_video_payload(model, prompt, extra)


async def create_video_generation(
    model: str,
    prompt: str,
    extra: Dict[str, Any],
    *,
    idempotency_key: Optional[str] = None,
) -> Dict[str, Any]:
    payload = build_video_generation_payload(model, prompt, extra)
    try:
        response = await (await _get_client()).post(
            "/videos/generations",
            cast_to=httpx.Response,
            body=payload,
            options=_request_options(idempotency_key),
        )
        result = await _read_json_response(response, "APIMart 视频响应格式错误")
    except Exception as exc:
        _raise_provider_error("APIMart 视频生成调用失败", exc)
    return result


def build_private_avatar_payload(
    assets: List[Dict[str, Any]],
    *,
    group_name: Optional[str] = None,
    group_id: Optional[str] = None,
    project_name: str = "story-ai",
    model: Optional[str] = None,
) -> Dict[str, Any]:
    if bool(group_name) == bool(group_id):
        raise AppException(
            "group_name 与 group_id 必须且只能传入一个",
            code=40012,
            status_code=400,
        )
    if not assets:
        raise AppException("人像素材不能为空", code=40012, status_code=400)
    if len(assets) > 20:
        raise AppException("人像素材单次最多提交 20 个", code=40012, status_code=400)

    normalized_assets: List[Dict[str, str]] = []
    for item in assets:
        url = str(item.get("url") or "").strip()
        name = str(item.get("name") or "").strip()
        if not url.startswith(("http://", "https://")) or not name:
            raise AppException(
                "人像素材必须包含公网可访问的 url 和 name",
                code=40012,
                status_code=400,
            )
        normalized_assets.append({"url": url, "name": name})

    payload: Dict[str, Any] = {
        "project_name": str(project_name or "story-ai").strip() or "story-ai",
        "asset_type": "Image",
        "assets": normalized_assets,
    }
    normalized_model = str(model or "").strip().lower()
    if normalized_model:
        if normalized_model != "seedance-2.5":
            raise AppException(
                "真人素材审核 model 当前仅支持 seedance-2.5",
                code=40012,
                status_code=400,
            )
        payload["model"] = normalized_model
    if group_id:
        payload["group_id"] = str(group_id).strip()
    else:
        payload["group"] = {"name": str(group_name).strip()}
    return payload


async def create_private_avatar_assets(
    assets: List[Dict[str, Any]],
    *,
    group_name: Optional[str] = None,
    group_id: Optional[str] = None,
    project_name: str = "story-ai",
    model: Optional[str] = None,
) -> Dict[str, Any]:
    payload = build_private_avatar_payload(
        assets,
        group_name=group_name,
        group_id=group_id,
        project_name=project_name,
        model=model,
    )
    endpoint = (
        "/seedance2/private-avatar/assets"
        if payload.get("model") == "seedance-2.5"
        else "/seedance2/private-avatar"
    )
    try:
        response = await (await _get_client()).post(
            endpoint,
            cast_to=httpx.Response,
            body=payload,
        )
        return await _read_json_response(response, "APIMart 人像素材审核响应格式错误")
    except Exception as exc:
        _raise_provider_error("APIMart 人像素材审核调用失败", exc)


def _can_fallback_to_chat(extra: Dict[str, Any], exc: APIStatusError) -> bool:
    return (
        exc.status_code in _RESPONSES_FALLBACK_STATUS_CODES
        and extra.get("tools") is None
        and not _request_contains_images(extra)
        and not any(_has_value(extra.get(key)) for key in _VIDEO_INPUT_KEYS)
    )


def _should_use_responses(extra: Dict[str, Any]) -> bool:
    capability = str(extra.get("capability") or "").strip().lower()
    return (
        capability == "responses"
        or extra.get("tools") is not None
        or _request_contains_images(extra)
    )


async def _create_streaming_chat_completion(
    model: str,
    prompt: str,
    extra: Dict[str, Any],
    *,
    idempotency_key: Optional[str] = None,
    on_text_delta: Optional[TextDeltaCallback] = None,
) -> Dict[str, Any]:
    payload = build_chat_completion_payload(model, prompt, extra)
    try:
        response = await (await _get_client()).post(
            "/chat/completions",
            cast_to=httpx.Response,
            body=payload,
            options=_request_options(idempotency_key),
            stream=True,
        )
    except Exception as exc:
        _raise_provider_error("APIMart 文本模型调用失败", exc)
    return await _consume_streaming_response(
        response,
        protocol="chat_completions",
        on_text_delta=on_text_delta,
    )


async def _create_non_streaming_chat_completion(
    model: str,
    prompt: str,
    extra: Dict[str, Any],
    *,
    idempotency_key: Optional[str] = None,
) -> Dict[str, Any]:
    payload = build_chat_completion_payload(model, prompt, extra)
    payload["stream"] = False
    try:
        response = await (await _get_client()).post(
            _non_streaming_chat_url(),
            cast_to=httpx.Response,
            body=payload,
            options=_request_options(idempotency_key),
            stream=False,
        )
        result = _unwrap_provider_payload(
            await _read_json_response(response, "APIMart 非流式文本响应格式错误")
        )
    except Exception as exc:
        _raise_provider_error("APIMart 非流式文本模型调用失败", exc)
    result["provider_api"] = "chat_completions_nostream"
    result["streamed"] = False
    return result


def _non_streaming_chat_url() -> str:
    parsed = urlsplit(_base_url())
    return urlunsplit((parsed.scheme, parsed.netloc, "/api/v1/chat/completions", "", ""))


def build_responses_payload(model: str, prompt: str, extra: Dict[str, Any]) -> Dict[str, Any]:
    model_id = str(model or "").strip()
    if not model_id:
        raise AppException("模型 ID 不能为空", code=40012, status_code=400)
    if any(_has_value(extra.get(key)) for key in _VIDEO_INPUT_KEYS):
        raise AppException(
            "APIMart Responses 当前仅支持文本和图片输入",
            code=40012,
            status_code=400,
        )

    messages = _responses_messages(prompt, extra)
    system_prompt = str(extra.get("system_prompt") or "").strip()
    if system_prompt:
        messages.insert(0, {"role": "system", "content": system_prompt})
    payload: Dict[str, Any] = {
        "model": model_id,
        "input": _normalize_responses_input(messages),
        "stream": True,
    }
    for key in _RESPONSES_REQUEST_KEYS:
        value = extra.get(key)
        if value is None:
            continue
        if key == "temperature":
            payload[key] = _normalize_float(key, value, 0, 2)
        elif key == "top_p":
            payload[key] = _normalize_float(key, value, 0, 1)
        elif key == "max_tokens":
            payload[key] = _normalize_positive_int(key, value)
        elif key == "tools":
            payload[key] = _normalize_tools(value)
    return payload


def _responses_messages(prompt: str, extra: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw_messages = extra.get("messages")
    if raw_messages is not None and not isinstance(raw_messages, list):
        raise AppException("messages 必须是数组", code=40012, status_code=400)
    if raw_messages and not all(isinstance(item, dict) for item in raw_messages):
        raise AppException("messages 每一项必须是对象", code=40012, status_code=400)
    messages = [dict(item) for item in (raw_messages or [])]
    if not messages:
        messages = [{"role": "user", "content": prompt}]

    image_urls = _collect_extra_image_urls(extra)
    if image_urls and not _messages_contain_images(messages):
        user_message = next(
            (item for item in reversed(messages) if item.get("role") == "user"),
            None,
        )
        if user_message is None:
            user_message = {"role": "user", "content": prompt}
            messages.append(user_message)
        content = user_message.get("content")
        if isinstance(content, str):
            normalized_content: List[Dict[str, Any]] = [{"type": "text", "text": content}]
        elif isinstance(content, list):
            normalized_content = list(content)
        else:
            normalized_content = []
        normalized_content.extend(
            {"type": "image_url", "image_url": {"url": url}} for url in image_urls
        )
        user_message["content"] = normalized_content
    return messages


def _request_contains_images(extra: Dict[str, Any]) -> bool:
    return _messages_contain_images(extra.get("messages")) or bool(_collect_extra_image_urls(extra))


def _collect_extra_image_urls(
    extra: Dict[str, Any],
    *,
    deduplicate: bool = True,
    validate: bool = True,
) -> List[str]:
    urls: List[str] = []
    for key in _IMAGE_INPUT_KEYS:
        value = extra.get(key)
        values = value if isinstance(value, list) else [value]
        for item in values:
            url = _extract_extra_image_url(item)
            if not url or (deduplicate and url in urls):
                continue
            if validate:
                _validate_image_url(url)
            urls.append(url)
    return urls


def _extract_extra_image_url(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    for key in ("url", "image_url", "oss_url"):
        nested = value.get(key)
        if isinstance(nested, str):
            return nested.strip()
        if isinstance(nested, dict) and isinstance(nested.get("url"), str):
            return nested["url"].strip()
    for key in ("data", "response", "file", "upload"):
        nested_url = _extract_extra_image_url(value.get(key))
        if nested_url:
            return nested_url
    return ""


async def create_multimodal_response(
    model: str,
    prompt: str,
    extra: Dict[str, Any],
    *,
    idempotency_key: Optional[str] = None,
    on_text_delta: Optional[TextDeltaCallback] = None,
) -> Dict[str, Any]:
    payload = build_responses_payload(model, prompt, extra)
    try:
        response = await (await _get_client()).post(
            "/responses",
            cast_to=httpx.Response,
            body=payload,
            options=_request_options(idempotency_key),
            stream=True,
        )
    except APIStatusError:
        raise
    except Exception as exc:
        _raise_provider_error("APIMart 多模态响应调用失败", exc)
    return await _consume_streaming_response(
        response,
        protocol="responses",
        on_text_delta=on_text_delta,
    )


def _request_options(idempotency_key: Optional[str]) -> Dict[str, Any]:
    return {"idempotency_key": idempotency_key} if idempotency_key else {}


def _image_request_options(model: str, idempotency_key: Optional[str]) -> Dict[str, Any]:
    options = _request_options(idempotency_key)
    if requires_stable_response_header(model):
        options["headers"] = {"X-APIMart-Response-Version": "2026-07-27"}
    return options


async def _read_json_response(
    response: httpx.Response,
    invalid_message: str,
) -> Dict[str, Any]:
    try:
        await response.aread()
        payload = response.json()
    finally:
        await response.aclose()
    if not isinstance(payload, dict):
        raise AppException(invalid_message, code=50231, status_code=502)
    return payload


async def _consume_streaming_response(
    response: httpx.Response,
    *,
    protocol: str,
    on_text_delta: Optional[TextDeltaCallback],
) -> Dict[str, Any]:
    text_parts: List[str] = []
    final_payload: Dict[str, Any] = {}
    try:
        content_type = str(response.headers.get("content-type") or "").lower()
        if "text/event-stream" not in content_type:
            await response.aread()
            payload = response.json()
            return _unwrap_provider_payload(payload)

        async for line in response.aiter_lines():
            payload = _parse_sse_data_line(line)
            if payload is None:
                continue
            event = _unwrap_provider_payload(payload)
            event_type = str(event.get("type") or "")
            if event_type in {"response.failed", "response.error", "error"}:
                raise AppException(
                    _stream_error_message(event),
                    code=50231,
                    status_code=502,
                )
            delta = _extract_stream_delta(event, protocol)
            if delta:
                text_parts.append(delta)
                if on_text_delta is not None:
                    await on_text_delta(delta)
            completed = event.get("response") if event_type == "response.completed" else None
            if isinstance(completed, dict):
                final_payload = completed
            elif not event_type or event_type.endswith(".done"):
                final_payload = event
    except AppException:
        raise
    except Exception as exc:
        if text_parts:
            raise AppException(
                "APIMart 流式响应中断，请重新发起生成",
                code=50231,
                status_code=502,
            ) from exc
        _raise_provider_error("APIMart 流式响应读取失败", exc)
    finally:
        await response.aclose()

    content = "".join(text_parts)
    if protocol == "chat_completions":
        return _build_chat_stream_result(final_payload, content)
    return _build_responses_stream_result(final_payload, content)


def _parse_sse_data_line(line: str) -> Optional[Dict[str, Any]]:
    value = str(line or "").strip()
    if not value or value.startswith(("event:", "id:", "retry:", ":")):
        return None
    if value.startswith("data:"):
        value = value[5:].strip()
    if not value or value == "[DONE]":
        return None
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise AppException("APIMart SSE 数据格式错误", code=50231, status_code=502) from exc
    if not isinstance(payload, dict):
        raise AppException("APIMart SSE 数据格式错误", code=50231, status_code=502)
    return payload


def _unwrap_provider_payload(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise AppException("APIMart 响应格式错误", code=50231, status_code=502)
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def _extract_stream_delta(event: Dict[str, Any], protocol: str) -> str:
    event_type = str(event.get("type") or "")
    if event_type in {"response.output_text.delta", "response.refusal.delta"}:
        return str(event.get("delta") or "")

    choices = event.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ""
    choice = choices[0]
    delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
    content = delta.get("content")
    if content:
        return str(content)
    if protocol == "responses":
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        if message.get("content"):
            return str(message["content"])
    return ""


def _build_responses_stream_result(final_payload: Dict[str, Any], content: str) -> Dict[str, Any]:
    result = dict(final_payload)
    if content:
        result["output_text"] = content
    if not content and not result:
        raise AppException("APIMart 未返回有效内容", code=50231, status_code=502)
    result.setdefault("object", "response")
    result["provider_api"] = "responses"
    result["streamed"] = True
    return result


def _build_chat_stream_result(final_payload: Dict[str, Any], content: str) -> Dict[str, Any]:
    result = dict(final_payload)
    if content:
        result["choices"] = [{"index": 0, "message": {"content": content}}]
    if not content and not result:
        raise AppException("APIMart 未返回有效内容", code=50231, status_code=502)
    result.setdefault("object", "chat.completion")
    result["provider_api"] = "chat_completions"
    result["streamed"] = True
    return result


def _stream_error_message(event: Dict[str, Any]) -> str:
    error = event.get("error")
    if isinstance(error, dict) and error.get("message"):
        return f"APIMart 流式响应失败：{error['message']}"
    response = event.get("response")
    if isinstance(response, dict):
        nested_error = response.get("error")
        if isinstance(nested_error, dict) and nested_error.get("message"):
            return f"APIMart 流式响应失败：{nested_error['message']}"
    return "APIMart 流式响应失败"


def _normalize_responses_input(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise AppException("Responses input 必须是非空数组", code=40012, status_code=400)
    result: List[Dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise AppException("Responses input 每一项必须是对象", code=40012, status_code=400)
        role = str(item.get("role") or "").strip()
        if role not in {"system", "user", "assistant"}:
            raise AppException("Responses input role 不正确", code=40012, status_code=400)
        result.append({"role": role, "content": _normalize_responses_content(item.get("content"))})
    return result


def _normalize_responses_content(value: Any) -> List[Dict[str, Any]]:
    if isinstance(value, str):
        if not value.strip():
            raise AppException("Responses 文本内容不能为空", code=40012, status_code=400)
        return [{"type": "input_text", "text": value}]
    if not isinstance(value, list) or not value:
        raise AppException("Responses content 必须是非空数组", code=40012, status_code=400)

    result: List[Dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise AppException("Responses content 每一项必须是对象", code=40012, status_code=400)
        content_type = str(item.get("type") or "").strip()
        if content_type in {"text", "input_text"}:
            text = str(item.get("text") or "")
            if not text:
                raise AppException("Responses 文本内容不能为空", code=40012, status_code=400)
            result.append({"type": "input_text", "text": text})
            continue
        if content_type in {"image_url", "input_image"}:
            image_url = _extract_image_url(item)
            _validate_image_url(image_url)
            result.append({"type": "input_image", "image_url": image_url})
            continue
        raise AppException("Responses content 类型不支持", code=40012, status_code=400)
    return result


def _messages_contain_images(value: Any) -> bool:
    if not isinstance(value, list):
        return False
    for message in value:
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            continue
        for item in message["content"]:
            if isinstance(item, dict) and item.get("type") in {"image_url", "input_image"}:
                return True
    return False


def _extract_image_url(item: Dict[str, Any]) -> str:
    value = item.get("image_url")
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return str(value.get("url") or "").strip()
    return ""


def _validate_image_url(value: str) -> None:
    if not value or not value.lower().startswith(_IMAGE_URL_PREFIXES):
        raise AppException(
            "图片必须是公开 HTTP(S) URL 或完整的 JPEG/PNG/GIF/WebP Data URI",
            code=40012,
            status_code=400,
        )
    if value.lower().startswith("data:image/") and len(value) > 28 * 1024 * 1024:
        raise AppException("Base64 图片不能超过 20MB", code=40012, status_code=400)


def _normalize_float(key: str, value: Any, lower: float, upper: float) -> float:
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise AppException(f"{key} 必须是数字", code=40012, status_code=400) from exc
    if normalized < lower or normalized > upper:
        raise AppException(f"{key} 必须在 {lower:g}-{upper:g} 之间", code=40012, status_code=400)
    return normalized


def _normalize_positive_int(key: str, value: Any) -> int:
    if isinstance(value, bool):
        raise AppException(f"{key} 必须是正整数", code=40012, status_code=400)
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise AppException(f"{key} 必须是正整数", code=40012, status_code=400) from exc
    if normalized <= 0:
        raise AppException(f"{key} 必须是正整数", code=40012, status_code=400)
    return normalized


def _normalize_tools(value: Any) -> List[Dict[str, Any]]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, dict) for item in value)
    ):
        raise AppException("tools 必须是非空对象数组", code=40012, status_code=400)
    return [dict(item) for item in value]


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


async def query_generation_task(task_id: str) -> Dict[str, Any]:
    normalized_task_id = str(task_id or "").strip()
    if not normalized_task_id:
        raise AppException("任务 ID 不能为空", code=40012, status_code=400)
    try:
        response = await (await _get_client()).get(
            f"/tasks/{quote(normalized_task_id, safe='')}",
            cast_to=httpx.Response,
            options={"query": {"language": "zh"}},
        )
        payload = await _read_json_response(response, "APIMart 任务响应格式错误")
    except Exception as exc:
        _raise_provider_error("APIMart 任务查询失败", exc)
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def _raise_provider_error(prefix: str, exc: Exception) -> None:
    if isinstance(exc, AppException):
        raise exc
    if isinstance(exc, APITimeoutError):
        raise AppException("模型调用超时", code=50206, status_code=502) from exc
    if isinstance(exc, APIConnectionError):
        raise AppException("无法连接模型服务", code=50202, status_code=502) from exc
    if isinstance(exc, APIStatusError):
        status_code = exc.status_code
        if status_code == 400:
            if _provider_error_identifier(exc) == "nsfw_content_detected":
                raise AppException(
                    "提示词或参考图片未通过内容审核，请调整后重试",
                    code=40019,
                    status_code=400,
                ) from exc
            raise AppException(
                "当前模型不支持所选参数组合，请调整参数后重试",
                code=40016,
                status_code=400,
            ) from exc
        if status_code in {401, 403}:
            raise AppException(
                "模型服务认证失败，请检查服务配置", code=50231, status_code=502
            ) from exc
        if status_code == 402:
            raise AppException(
                "模型服务账户余额不足，请检查服务配置", code=40018, status_code=400
            ) from exc
        if status_code in {429, 500, 502, 503, 504}:
            raise AppException("模型服务繁忙，请稍后再试", code=50204, status_code=502) from exc
    if isinstance(exc, (json.JSONDecodeError, ValueError)):
        raise AppException("模型响应格式错误", code=50231, status_code=502) from exc
    raise AppException(prefix, code=50204, status_code=502) from exc


def _provider_error_identifier(exc: APIStatusError) -> str:
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        return ""
    error = body.get("error")
    if isinstance(error, dict):
        body = error
    for key in ("code", "type"):
        value = body.get(key)
        if value:
            return str(value).strip().lower()
    return ""
