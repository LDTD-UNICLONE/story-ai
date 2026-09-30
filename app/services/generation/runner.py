import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from app.core.exceptions import AppException
from app.core.logging import log_extra
from app.integrations import apimart
from app.integrations.apimart import APIMART_VENDOR
from app.integrations.apimart_video_specs import (
    merge_video_capabilities as merge_apimart_video_capabilities,
)
from app.integrations import comfly
from app.integrations.comfly_video_specs import merge_video_capabilities
from app.integrations import volcengine_ark
from app.integrations.volcengine_ark_video_specs import (
    VOLCENGINE_ARK_VENDOR,
    merge_video_capabilities as merge_ark_video_capabilities,
)
from app.models.ai_model import AiModel
from app.services.models.configuration import (
    ensure_model_available,
    model_request_capabilities,
)


logger = logging.getLogger(__name__)


@dataclass
class ModelRunResult:
    content: str
    extra: Dict[str, Any] = field(default_factory=dict)


class EmptyModelContentError(AppException):
    def __init__(self, message: str, response_summary: Dict[str, Any]) -> None:
        super().__init__(message, code=50231, status_code=502)
        self.provider_response_summary = response_summary


async def run_model(
    ai_model: AiModel,
    generation_type: str,
    prompt: str,
    extra: Dict[str, Any],
    idempotency_key: Optional[str] = None,
    on_text_delta: Optional[apimart.TextDeltaCallback] = None,
    text_stream: bool = False,
) -> ModelRunResult:
    ensure_model_available(ai_model)
    if _should_use_volcengine_ark(ai_model, generation_type):
        return await _run_volcengine_ark(ai_model, generation_type, prompt, extra)
    if _should_use_apimart(ai_model):
        return await _run_apimart(
            ai_model,
            generation_type,
            prompt,
            extra,
            idempotency_key,
            on_text_delta,
            text_stream,
        )
    if _should_use_comfly(ai_model):
        return await _run_comfly(ai_model, generation_type, prompt, extra, idempotency_key)

    raise AppException("暂不支持该模型", code=40005, status_code=400)


async def query_model_task(
    ai_model: AiModel,
    generation_type: str,
    task_id: str,
) -> ModelRunResult:
    if _should_use_volcengine_ark(ai_model, generation_type):
        return await _query_volcengine_ark_task(generation_type, task_id)
    if _should_use_apimart(ai_model):
        return await _query_apimart_task(generation_type, task_id)
    if _should_use_comfly(ai_model):
        return await _query_comfly_task(generation_type, task_id)

    raise AppException("暂不支持该模型", code=40005, status_code=400)


def validate_model_request(
    ai_model: AiModel,
    generation_type: str,
    prompt: str,
    extra: Dict[str, Any],
) -> None:
    """Validate compiled business inputs locally before charging or creating a task."""
    if generation_type == "video":
        validate_video_request(ai_model, prompt, extra)
        return
    ensure_model_available(ai_model)
    if generation_type not in {"text", "image"}:
        raise AppException("不支持的生成类型", code=40004, status_code=400)
    if _should_use_apimart(ai_model):
        provider = apimart
    elif _should_use_comfly(ai_model):
        provider = comfly
    else:
        raise AppException("暂不支持该模型", code=40005, status_code=400)
    if generation_type == "text":
        provider.validate_chat_completion_request(ai_model.model_id, prompt, extra)
    else:
        provider.validate_image_request(ai_model.model_id, prompt, extra)


def validate_video_request(ai_model: AiModel, prompt: str, extra: Dict[str, Any]) -> None:
    """Build the same provider request as the worker, without making an external call."""
    ensure_model_available(ai_model)
    provider, provider_extra = _video_provider_request(ai_model, extra)
    provider.build_video_generation_payload(ai_model.model_id, prompt, provider_extra)


def _video_provider_request(ai_model: AiModel, extra: Dict[str, Any]):
    if _should_use_volcengine_ark(ai_model, "video"):
        provider, merge = volcengine_ark, merge_ark_video_capabilities
    elif _should_use_apimart(ai_model):
        provider, merge = apimart, merge_apimart_video_capabilities
    elif _should_use_comfly(ai_model):
        provider, merge = comfly, merge_video_capabilities
    else:
        raise AppException("暂不支持该模型", code=40005, status_code=400)
    provider_extra = dict(extra)
    provider_extra["_model_capabilities"] = merge(
        ai_model.model_id, model_request_capabilities(ai_model)
    )
    return provider, provider_extra


async def _run_comfly(
    ai_model: AiModel,
    generation_type: str,
    prompt: str,
    extra: Dict[str, Any],
    idempotency_key: Optional[str],
) -> ModelRunResult:
    if generation_type == "text":
        payload = await comfly.create_chat_completion(
            ai_model.model_id,
            prompt,
            extra,
            idempotency_key=idempotency_key,
        )
        content = _extract_chat_content(payload) or _extract_media_content(payload)
        if not content:
            response_summary = _chat_response_summary(payload)
            logger.warning(
                "Comfly chat completion returned empty content: response_summary=%s",
                json.dumps(response_summary, ensure_ascii=False, default=str),
                extra=log_extra(
                    event="comfly_chat_empty_content",
                    model_id=ai_model.model_id,
                    response_summary=response_summary,
                ),
            )
            raise EmptyModelContentError(_empty_content_message(payload), response_summary)
        return ModelRunResult(
            content=content,
            extra={
                "chat_capability": extra.get("capability") or extra.get("chat_mode") or "chat",
                "provider_response": payload,
            },
        )

    if generation_type == "image":
        payload = await comfly.create_image_generation(
            ai_model.model_id,
            prompt,
            extra,
            idempotency_key=idempotency_key,
        )
        image_mode = str(extra.get("image_mode") or extra.get("capability") or "generation")
        task_id = _extract_task_id(payload)
        if task_id:
            content = f"图像生成任务已提交：{task_id}"
            return ModelRunResult(
                content=content,
                extra={
                    "task_id": task_id, "task_status": _extract_status(payload), "provider_response": payload,
                },
            )
        content = _extract_chat_content(payload) or _extract_media_content(payload)
        if not content:
            raise AppException(
                "图像模型响应格式错误：未返回 task_id 或图片结果", code=50231, status_code=502
            )
        return ModelRunResult(
            content=content, extra={"image_mode": image_mode, "provider_response": payload}
        )

    if generation_type == "video":
        _, provider_extra = _video_provider_request(ai_model, extra)
        payload = await comfly.create_video_generation(
            ai_model.model_id,
            prompt,
            provider_extra,
            idempotency_key=idempotency_key,
        )
        video_mode = str(extra.get("video_mode") or extra.get("capability") or "generation")
        task_id = _extract_task_id(payload)
        if not task_id:
            raise AppException("视频模型响应格式错误：未返回 task_id", code=50232, status_code=502)
        content = f"视频生成任务已提交：{task_id}"
        return ModelRunResult(
            content=content,
            extra={
                "task_id": task_id, "task_status": _extract_status(payload), "video_mode": video_mode, "provider_response": payload,
            },
        )

    raise AppException("不支持的生成类型", code=40004, status_code=400)


async def _run_volcengine_ark(
    ai_model: AiModel,
    generation_type: str,
    prompt: str,
    extra: Dict[str, Any],
) -> ModelRunResult:
    if generation_type != "video":
        raise AppException("当前模型仅支持视频生成", code=40007, status_code=400)

    _, provider_extra = _video_provider_request(ai_model, extra)
    payload = await volcengine_ark.create_video_generation(
        ai_model.model_id, prompt, provider_extra
    )
    video_mode = str(extra.get("video_mode") or extra.get("capability") or "generation")
    task_id = _extract_task_id(payload)
    content = f"视频生成任务已提交：{task_id}" if task_id else "视频生成任务已提交"
    if not task_id:
        content = _extract_media_content(payload)
    return ModelRunResult(
        content=content,
        extra={
            "task_id": task_id,
            "task_status": _extract_status(payload),
            "video_mode": video_mode,
            "provider_response": payload,
        },
    )


async def _run_apimart(
    ai_model: AiModel,
    generation_type: str,
    prompt: str,
    extra: Dict[str, Any],
    idempotency_key: Optional[str],
    on_text_delta: Optional[apimart.TextDeltaCallback],
    text_stream: bool,
) -> ModelRunResult:
    if generation_type == "image":
        payload = await apimart.create_image_generation(
            ai_model.model_id,
            prompt,
            extra,
            idempotency_key=idempotency_key,
        )
        task_id = _extract_task_id(payload)
        if task_id:
            return ModelRunResult(
                content=f"图像生成任务已提交：{task_id}",
                extra={
                    "task_id": task_id,
                    "task_status": _extract_status(payload),
                    "image_mode": str(
                        extra.get("image_mode") or extra.get("capability") or "generation"
                    ),
                    "provider_response": payload,
                },
            )
        content = _extract_media_content(payload)
        if not content:
            raise AppException(
                "APIMart 图像响应格式错误：未返回 task_id 或图片结果",
                code=50231,
                status_code=502,
            )
        return ModelRunResult(
            content=content,
            extra={
                "image_mode": str(
                    extra.get("image_mode") or extra.get("capability") or "generation"
                ),
                "provider_response": payload,
            },
        )

    if generation_type == "video":
        _, provider_extra = _video_provider_request(ai_model, extra)
        payload = await apimart.create_video_generation(
            ai_model.model_id,
            prompt,
            provider_extra,
            idempotency_key=idempotency_key,
        )
        task_id = _extract_task_id(payload)
        if not task_id:
            raise AppException(
                "APIMart 视频响应格式错误：未返回 task_id",
                code=50232,
                status_code=502,
            )
        return ModelRunResult(
            content=f"视频生成任务已提交：{task_id}",
            extra={
                "task_id": task_id,
                "task_status": _extract_status(payload),
                "video_mode": str(
                    extra.get("video_mode") or extra.get("capability") or "generation"
                ),
                "provider_response": payload,
            },
        )

    if generation_type != "text":
        raise AppException("不支持的生成类型", code=40004, status_code=400)
    kwargs: Dict[str, Any] = {
        "idempotency_key": idempotency_key,
        "stream": text_stream,
    }
    if on_text_delta is not None:
        kwargs["on_text_delta"] = on_text_delta
    payload = await apimart.create_chat_completion(
        ai_model.model_id, prompt, extra, **kwargs
    )
    content = _extract_chat_content(payload) or _extract_media_content(payload)
    if not content:
        raise EmptyModelContentError(
            _empty_content_message(payload), _chat_response_summary(payload)
        )
    return ModelRunResult(
        content=content,
        extra={
            "chat_capability": extra.get("capability") or extra.get("chat_mode") or "chat",
            "provider_api": payload.get("provider_api")
            or (
                "chat_completions"
                if text_stream
                else "chat_completions_nostream"
            ),
            "streamed": bool(payload.get("streamed")),
            "provider_response": payload,
        },
    )


async def _query_comfly_task(generation_type: str, task_id: str) -> ModelRunResult:
    if generation_type == "image":
        payload = await comfly.query_image_generation(task_id)
        content = _extract_media_content(payload)
        return ModelRunResult(
            content=content,
            extra={
                "task_id": _extract_task_id(payload) or task_id, "task_status": _extract_status(payload), "provider_response": payload,
            },
        )

    if generation_type == "video":
        payload = await comfly.query_video_generation(task_id)
        content = _extract_media_content(payload)
        return ModelRunResult(
            content=content,
            extra={
                "task_id": _extract_task_id(payload) or task_id, "task_status": _extract_status(payload), "provider_response": payload,
            },
        )

    raise AppException("该生成类型没有任务查询接口", code=40006, status_code=400)


async def _query_volcengine_ark_task(generation_type: str, task_id: str) -> ModelRunResult:
    if generation_type == "video":
        payload = await volcengine_ark.query_video_generation(task_id)
        content = _extract_media_content(payload)
        return ModelRunResult(
            content=content,
            extra={
                "task_id": _extract_task_id(payload) or task_id,
                "task_status": _extract_status(payload),
                "provider_response": payload,
            },
        )

    raise AppException("该生成类型没有任务查询接口", code=40006, status_code=400)


async def _query_apimart_task(generation_type: str, task_id: str) -> ModelRunResult:
    if generation_type not in {"image", "video"}:
        raise AppException("该生成类型没有任务查询接口", code=40006, status_code=400)
    payload = await apimart.query_generation_task(task_id)
    return ModelRunResult(
        content=_extract_media_content(payload),
        extra={
            "task_id": _extract_task_id(payload) or task_id,
            "task_status": _extract_status(payload),
            "provider_response": payload,
        },
    )


def _should_use_volcengine_ark(ai_model: AiModel, generation_type: str) -> bool:
    return generation_type == "video" and ai_model.vendor == VOLCENGINE_ARK_VENDOR


def _should_use_comfly(ai_model: AiModel) -> bool:
    return ai_model.vendor in {"comfly", "模型服务"}


def _should_use_apimart(ai_model: AiModel) -> bool:
    return ai_model.vendor == APIMART_VENDOR


def _extract_chat_content(payload: Dict[str, Any]) -> str:
    responses_content = _extract_responses_content(payload)
    if responses_content:
        return responses_content

    choices = payload.get("choices") or []
    if choices:
        first_choice = choices[0] if isinstance(choices[0], dict) else {}
        message = first_choice.get("message") or {}
        if first_choice.get("text"):
            return str(first_choice["text"])
        delta = first_choice.get("delta") if isinstance(first_choice.get("delta"), dict) else {}
        if delta.get("content"):
            return str(delta["content"])
        content = message.get("content")
        if isinstance(content, list):
            text_parts = []
            media_parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") in {"text", "output_text"}:
                    text_parts.append(str(item.get("text") or ""))
                elif isinstance(item, dict) and item.get("type") in {"image_url", "video_url"}:
                    media_url = item.get("image_url") or item.get("video_url")
                    if isinstance(media_url, dict) and media_url.get("url"):
                        media_parts.append(str(media_url["url"]))
                    elif media_url:
                        media_parts.append(str(media_url))
                elif isinstance(item, str):
                    text_parts.append(item)
            return "".join(text_parts) or ",".join(media_parts)
        if content:
            return str(content)
        for key in ("reasoning_content", "reasoning", "refusal"):
            value = message.get(key)
            if value:
                return str(value)
        tool_calls = message.get("tool_calls")
        if tool_calls:
            return json.dumps({"tool_calls": tool_calls}, ensure_ascii=False)
    return ""


def _extract_responses_content(payload: Dict[str, Any]) -> str:
    output_text = payload.get("output_text")
    if output_text:
        return str(output_text)

    output = payload.get("output")
    if not isinstance(output, list):
        return ""

    text_parts: List[str] = []
    refusals: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "")
        if item_type in {"function_call", "tool_call"}:
            tool_calls.append(item)
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = str(part.get("type") or "")
            if part_type in {"output_text", "text"} and part.get("text"):
                text_parts.append(str(part["text"]))
            elif part_type == "refusal" and part.get("refusal"):
                refusals.append(str(part["refusal"]))

    if text_parts:
        return "".join(text_parts)
    if refusals:
        return "".join(refusals)
    if tool_calls:
        return json.dumps({"tool_calls": tool_calls}, ensure_ascii=False)
    return ""


def _extract_media_content(payload: Dict[str, Any]) -> str:
    output_urls = _extract_provider_output_urls(payload)
    if output_urls:
        return ",".join(output_urls)

    for key in ("url", "video_url", "image_url", "b64_json", "output", "result"):
        value = _find_first_value(payload, (key,))
        if value:
            if isinstance(value, list):
                urls = _collect_media_urls(value)
                return ",".join(urls) if urls else ",".join(str(item) for item in value)
            if isinstance(value, dict):
                urls = _collect_media_urls(value)
                return ",".join(urls) if urls else str(value)
            return str(value)
    return "生成任务处理中" if _extract_task_id(payload) else ""


def _extract_task_id(payload: Dict[str, Any]) -> Optional[str]:
    value = _find_first_value(payload, ("task_id", "taskId", "taskID", "job_id", "jobId"))
    if value:
        return str(value)
    object_name = str(payload.get("object") or "")
    status = payload.get("status") or payload.get("state")
    plain_id = payload.get("id")
    if plain_id and status and object_name not in {"chat.completion", "chat.completion.chunk"}:
        return str(plain_id)
    data = payload.get("data")
    if isinstance(data, dict):
        data_id = data.get("id")
        data_status = data.get("status") or data.get("state")
        if data_id and data_status:
            return str(data_id)
    return None


def _empty_content_message(payload: Dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    finish_reason = None
    if choices and isinstance(choices[0], dict):
        finish_reason = choices[0].get("finish_reason")
    if finish_reason == "length":
        return "模型未返回有效内容：输出达到长度限制，请调大 max_tokens 或更换模型参数后重试"
    return "模型未返回有效内容"


def _chat_response_summary(payload: Dict[str, Any]) -> Dict[str, Any]:
    choices = payload.get("choices") if isinstance(payload.get("choices"), list) else []
    first_choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    message = first_choice.get("message") if isinstance(first_choice.get("message"), dict) else {}
    return {
        "id": payload.get("id"),
        "object": payload.get("object"),
        "model": payload.get("model"),
        "response_keys": sorted(str(key) for key in payload.keys()),
        "choices_count": len(choices),
        "finish_reason": first_choice.get("finish_reason"),
        "message_keys": sorted(str(key) for key in message.keys()),
        "usage": payload.get("usage"),
    }


def _extract_status(payload: Dict[str, Any]) -> Optional[str]:
    value = _find_first_value(payload, ("status", "state"))
    return str(value) if value else None


def _extract_provider_output_urls(payload: Dict[str, Any]) -> List[str]:
    data = payload.get("data")
    candidates: List[Any] = []
    if isinstance(data, dict):
        candidates.extend(
            [
                data.get("output"),
                data.get("outputs"),
                data.get("url"),
                data.get("urls"),
                data.get("image_url"),
                data.get("image_urls"),
                data.get("video_url"),
                data.get("video_urls"),
                data.get("result"),
            ]
        )
    candidates.extend(
        [
            payload.get("output"),
            payload.get("outputs"),
            payload.get("url"),
            payload.get("urls"),
            payload.get("image_url"),
            payload.get("image_urls"),
            payload.get("video_url"),
            payload.get("video_urls"),
            payload.get("result"),
        ]
    )

    urls: List[str] = []
    for candidate in candidates:
        urls.extend(_collect_media_urls(candidate))
    return _dedupe(urls)


def _collect_media_urls(value: Any) -> List[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        urls: List[str] = []
        for key in (
            "url",
            "uri",
            "image_url",
            "video_url",
            "file_url",
            "oss_url",
            "cdn_url",
            "cover_url",
            "b64_json",
        ):
            urls.extend(_collect_media_urls(value.get(key)))
        for key in ("output", "outputs", "result", "results", "data", "images", "videos"):
            urls.extend(_collect_media_urls(value.get(key)))
        return urls
    if isinstance(value, list):
        urls: List[str] = []
        for item in value:
            urls.extend(_collect_media_urls(item))
        return urls
    return []


def _dedupe(values: Iterable[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for value in values:
        normalized = str(value).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _find_first_value(data: Any, keys: Iterable[str]) -> Any:
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if value not in (None, ""):
                return value
        for value in data.values():
            nested = _find_first_value(value, keys)
            if nested not in (None, ""):
                return nested
    if isinstance(data, list):
        for item in data:
            nested = _find_first_value(item, keys)
            if nested not in (None, ""):
                return nested
    return None
