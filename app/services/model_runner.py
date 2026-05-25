from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional

from app.core.exceptions import AppException
from app.integrations import comfly
from app.integrations.comfly_video_specs import merge_video_capabilities
from app.integrations import volcengine_ark
from app.integrations.volcengine_ark_video_specs import (
    VOLCENGINE_ARK_VENDOR,
    is_volcengine_ark_video_model,
    merge_video_capabilities as merge_ark_video_capabilities,
)
from app.models.ai_model import AiModel


@dataclass
class ModelRunResult:
    content: str
    extra: Dict[str, Any] = field(default_factory=dict)


async def run_model(
    ai_model: AiModel,
    generation_type: str,
    prompt: str,
    extra: Dict[str, Any],
) -> ModelRunResult:
    if _should_use_volcengine_ark(ai_model, generation_type):
        return await _run_volcengine_ark(ai_model, generation_type, prompt, extra)
    if ai_model.vendor == "comfly":
        return await _run_comfly(ai_model, generation_type, prompt, extra)

    raise AppException("暂不支持该模型", code=40005, status_code=400)


async def query_model_task(
    ai_model: AiModel,
    generation_type: str,
    task_id: str,
) -> ModelRunResult:
    if _should_use_volcengine_ark(ai_model, generation_type):
        return await _query_volcengine_ark_task(generation_type, task_id)
    if ai_model.vendor == "comfly":
        return await _query_comfly_task(generation_type, task_id)

    raise AppException("暂不支持该模型", code=40005, status_code=400)


async def _run_comfly(
    ai_model: AiModel,
    generation_type: str,
    prompt: str,
    extra: Dict[str, Any],
) -> ModelRunResult:
    if generation_type == "text":
        payload = await comfly.create_chat_completion(ai_model.model_id, prompt, extra)
        content = _extract_chat_content(payload) or _extract_media_content(payload)
        if not content:
            content = _empty_content_message(payload)
        return ModelRunResult(
            content=content,
            extra={
                "chat_capability": extra.get("capability") or extra.get("chat_mode") or "chat",
                "provider_response": payload,
            },
        )

    if generation_type == "image":
        payload = await comfly.create_image_generation(ai_model.model_id, prompt, extra)
        image_mode = str(extra.get("image_mode") or extra.get("capability") or "generation")
        task_id = _extract_task_id(payload)
        if task_id:
            content = f"图像生成任务已提交：{task_id}"
            return ModelRunResult(
                content=content,
                extra={"task_id": task_id, "task_status": _extract_status(payload), "provider_response": payload},
            )
        content = _extract_chat_content(payload) or _extract_media_content(payload)
        return ModelRunResult(content=content, extra={"image_mode": image_mode, "provider_response": payload})

    if generation_type == "video":
        provider_extra = dict(extra)
        provider_extra["_model_capabilities"] = merge_video_capabilities(
            ai_model.model_id,
            ai_model.capabilities or {},
        )
        payload = await comfly.create_video_generation(ai_model.model_id, prompt, provider_extra)
        video_mode = str(extra.get("video_mode") or extra.get("capability") or "generation")
        task_id = _extract_task_id(payload)
        content = f"视频生成任务已提交：{task_id}" if task_id else "视频生成任务已提交"
        if not task_id:
            content = _extract_chat_content(payload) or _extract_media_content(payload)
        return ModelRunResult(
            content=content,
            extra={"task_id": task_id, "task_status": _extract_status(payload), "video_mode": video_mode, "provider_response": payload},
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

    provider_extra = dict(extra)
    provider_extra["_model_capabilities"] = merge_ark_video_capabilities(ai_model.capabilities or {})
    payload = await volcengine_ark.create_video_generation(ai_model.model_id, prompt, provider_extra)
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


async def _query_comfly_task(generation_type: str, task_id: str) -> ModelRunResult:
    if generation_type == "image":
        payload = await comfly.query_image_generation(task_id)
        content = _extract_media_content(payload)
        return ModelRunResult(
            content=content,
            extra={"task_id": _extract_task_id(payload) or task_id, "task_status": _extract_status(payload), "provider_response": payload},
        )

    if generation_type == "video":
        payload = await comfly.query_video_generation(task_id)
        content = _extract_media_content(payload)
        return ModelRunResult(
            content=content,
            extra={"task_id": _extract_task_id(payload) or task_id, "task_status": _extract_status(payload), "provider_response": payload},
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


def _should_use_volcengine_ark(ai_model: AiModel, generation_type: str) -> bool:
    return generation_type == "video" and (
        ai_model.vendor == VOLCENGINE_ARK_VENDOR or is_volcengine_ark_video_model(ai_model.model_id)
    )


def _extract_chat_content(payload: Dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    if choices:
        message = choices[0].get("message") or {}
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
    return ""


def _extract_media_content(payload: Dict[str, Any]) -> str:
    for key in ("url", "video_url", "image_url", "b64_json", "output", "result"):
        value = _find_first_value(payload, (key,))
        if value:
            if isinstance(value, list):
                return ",".join(str(item) for item in value)
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


def _extract_status(payload: Dict[str, Any]) -> Optional[str]:
    value = _find_first_value(payload, ("status", "state"))
    return str(value) if value else None


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
