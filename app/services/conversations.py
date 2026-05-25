from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import beijing_datetime
from app.core.exceptions import AppException
from app.models.ai_model import AiModel
from app.models.conversation import Conversation, ConversationMessage
from app.models.task_record import UserTaskRecord
from app.models.user import User
from app.schemas.conversation import ConversationCreateRequest, ConversationSendMessageRequest, ConversationUpdateRequest
from app.services.model_runner import ModelRunResult, query_model_task
from app.services.generated_media import persist_generated_media_to_oss
from app.services.model_points import calculate_model_points_cost
from app.services.points import change_user_points, consume_user_points
from app.services.task_records import (
    create_user_task_record,
    expire_stale_task_record,
    reconcile_provider_task_result,
)
from app.tasks.model_generation import run_conversation_generation


SUPPORTED_CONVERSATION_TYPES = {"text", "image", "video"}
DEFAULT_TEXT_CONTEXT_MESSAGE_LIMIT = 20
CONVERSATION_VIDEO_MODE_TO_PROVIDER_MODE = {
    "reference": "image_to_video",
    "first_last_frame": "first_last_frame",
}
TEXT_MULTIMODAL_MEDIA_KEYS = (
    "images",
    "image",
    "image_url",
    "image_urls",
    "videos",
    "video",
    "video_url",
    "video_urls",
)


async def get_enabled_conversation_model_or_404(
    db: AsyncSession,
    ai_model_id: UUID,
    conversation_type: str,
) -> AiModel:
    if conversation_type not in SUPPORTED_CONVERSATION_TYPES:
        raise AppException("不支持的对话类型", code=40004, status_code=400)

    result = await db.execute(
        select(AiModel).where(
            AiModel.id == ai_model_id,
            AiModel.is_enabled.is_(True),
            AiModel.model_type == conversation_type,
        )
    )
    ai_model = result.scalar_one_or_none()
    if ai_model is None:
        raise AppException("对话模型不存在、未启用或类型不匹配", code=40404, status_code=404)
    return ai_model


async def list_conversations(
    db: AsyncSession,
    user_id: UUID,
    page: int,
    page_size: int,
) -> Tuple[List[Conversation], int]:
    count_result = await db.execute(
        select(func.count())
        .select_from(Conversation)
        .where(Conversation.user_id == user_id, Conversation.is_enabled.is_(True))
    )
    total = count_result.scalar_one()

    result = await db.execute(
        select(Conversation)
        .where(Conversation.user_id == user_id, Conversation.is_enabled.is_(True))
        .order_by(Conversation.updated_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(result.scalars().all()), total


async def get_conversation_or_404(
    db: AsyncSession,
    conversation_id: UUID,
    user_id: UUID,
) -> Conversation:
    result = await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == user_id,
            Conversation.is_enabled.is_(True),
        )
    )
    conversation = result.scalar_one_or_none()
    if conversation is None:
        raise AppException("会话不存在", code=40405, status_code=404)
    return conversation


async def create_conversation(
    db: AsyncSession,
    user: User,
    payload: ConversationCreateRequest,
) -> Conversation:
    await get_enabled_conversation_model_or_404(db, payload.ai_model_id, payload.conversation_type)
    conversation = Conversation(
        user_id=user.id,
        title=payload.title,
        conversation_type=payload.conversation_type,
        ai_model_id=payload.ai_model_id,
        is_enabled=True,
    )
    db.add(conversation)
    await db.commit()
    await db.refresh(conversation)
    return conversation


async def delete_conversation(
    db: AsyncSession,
    conversation_id: UUID,
    user_id: UUID,
) -> Conversation:
    conversation = await get_conversation_or_404(db, conversation_id, user_id)
    conversation.is_enabled = False
    conversation.updated_at = beijing_datetime()
    await db.commit()
    await db.refresh(conversation)
    return conversation


async def update_conversation(
    db: AsyncSession,
    conversation_id: UUID,
    user_id: UUID,
    payload: ConversationUpdateRequest,
) -> Conversation:
    conversation = await get_conversation_or_404(db, conversation_id, user_id)
    conversation.title = payload.title
    conversation.updated_at = beijing_datetime()
    await db.commit()
    await db.refresh(conversation)
    return conversation


async def list_conversation_messages(
    db: AsyncSession,
    conversation_id: UUID,
    user_id: UUID,
    page: int,
    page_size: int,
    order: str = "desc",
) -> Tuple[List[ConversationMessage], int]:
    await get_conversation_or_404(db, conversation_id, user_id)

    count_result = await db.execute(
        select(func.count())
        .select_from(ConversationMessage)
        .where(ConversationMessage.conversation_id == conversation_id)
    )
    total = count_result.scalar_one()

    order_by = ConversationMessage.created_at.desc() if order == "desc" else ConversationMessage.created_at.asc()
    result = await db.execute(
        select(ConversationMessage)
        .where(ConversationMessage.conversation_id == conversation_id)
        .order_by(order_by)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    messages = list(result.scalars().all())
    await _reconcile_visible_message_tasks(db, messages, conversation_id, user_id)
    return messages, total


async def get_conversation_generation_task_status(
    db: AsyncSession,
    conversation_id: UUID,
    user_id: UUID,
    task_record_id: UUID,
) -> Tuple[UserTaskRecord, Optional[ConversationMessage]]:
    await get_conversation_or_404(db, conversation_id, user_id)
    result = await db.execute(
        select(UserTaskRecord).where(
            UserTaskRecord.id == task_record_id,
            UserTaskRecord.user_id == user_id,
            UserTaskRecord.business_type == "conversation",
            UserTaskRecord.business_id == conversation_id,
        )
    )
    task_record = result.scalar_one_or_none()
    if task_record is None:
        raise AppException("任务记录不存在", code=40406, status_code=404)
    await expire_stale_task_record(db, task_record)
    await reconcile_provider_task_result(db, task_record)
    await db.refresh(task_record)

    assistant_message_id = (task_record.extra or {}).get("assistant_message_id")
    assistant_message = None
    parsed_assistant_message_id = _parse_uuid(assistant_message_id)
    if parsed_assistant_message_id:
        assistant_message = await db.get(ConversationMessage, parsed_assistant_message_id)

    if assistant_message is not None and task_record.status in {"success", "failed"}:
        await _sync_assistant_message_from_task_record(db, assistant_message, task_record)

    return task_record, assistant_message


def conversation_task_content(
    task_record: UserTaskRecord,
    assistant_message: Optional[ConversationMessage],
) -> Optional[str]:
    if task_record.status in {"success", "failed"} and task_record.result:
        return _terminal_assistant_content(task_record)
    if assistant_message is not None:
        return assistant_message.content
    return task_record.result


def _terminal_assistant_content(task_record: UserTaskRecord) -> Optional[str]:
    if not task_record.result:
        return None
    if task_record.status == "failed":
        return f"任务执行失败：{task_record.result}"
    return task_record.result


def _parse_uuid(value: Any) -> Optional[UUID]:
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


async def _reconcile_visible_message_tasks(
    db: AsyncSession,
    messages: List[ConversationMessage],
    conversation_id: UUID,
    user_id: UUID,
) -> None:
    reconciled_count = 0
    for message in messages:
        if reconciled_count >= 5:
            return
        if message.role != "assistant":
            continue
        if (message.extra or {}).get("task_status") not in {"pending", "running"}:
            continue
        task_record_id = _parse_uuid((message.extra or {}).get("task_record_id"))
        if task_record_id is None:
            continue
        task_record = await db.get(UserTaskRecord, task_record_id)
        if (
            task_record is None
            or task_record.user_id != user_id
            or task_record.business_type != "conversation"
            or task_record.business_id != conversation_id
        ):
            continue
        await expire_stale_task_record(db, task_record)
        await reconcile_provider_task_result(db, task_record)
        await db.refresh(task_record)
        if task_record.status in {"success", "failed"}:
            await _sync_assistant_message_from_task_record(db, message, task_record)
        await db.refresh(message)
        reconciled_count += 1


async def _sync_assistant_message_from_task_record(
    db: AsyncSession,
    assistant_message: ConversationMessage,
    task_record: UserTaskRecord,
) -> None:
    expected_status = task_record.status
    current_status = (assistant_message.extra or {}).get("task_status")
    expected_content = _terminal_assistant_content(task_record)
    should_sync_content = bool(expected_content and assistant_message.content != expected_content)
    if current_status == expected_status and not should_sync_content:
        return

    assistant_message.extra = {
        **(assistant_message.extra or {}),
        **((task_record.extra or {}).get("assistant_message_extra") or {}),
        "task_status": expected_status,
        "task_record_id": str(task_record.id),
    }
    if task_record.status == "failed":
        assistant_message.extra = {
            **(assistant_message.extra or {}),
            "failed_reason": task_record.result,
        }
    if expected_content:
        assistant_message.content = expected_content
    await db.commit()
    await db.refresh(task_record)
    await db.refresh(assistant_message)


async def send_conversation_message(
    db: AsyncSession,
    conversation_id: UUID,
    user: User,
    payload: ConversationSendMessageRequest,
) -> Tuple[ConversationMessage, ConversationMessage, int]:
    conversation = await get_conversation_or_404(db, conversation_id, user.id)
    selected_ai_model_id = payload.ai_model_id or conversation.ai_model_id
    ai_model = await get_enabled_conversation_model_or_404(
        db,
        selected_ai_model_id,
        conversation.conversation_type,
    )
    conversation_db_id = conversation.id
    conversation_title = conversation.title
    conversation_type = conversation.conversation_type
    ai_model_db_id = ai_model.id
    ai_model_nickname = ai_model.nickname
    ai_model_points_cost = calculate_model_points_cost(ai_model)
    if conversation.ai_model_id != ai_model_db_id:
        conversation.ai_model_id = ai_model_db_id

    points_transaction = None
    if ai_model_points_cost > 0:
        points_transaction = await consume_user_points(
            db,
            user_id=user.id,
            amount=ai_model_points_cost,
            remark=f"对话模型调用：{ai_model_nickname}",
            auto_commit=False,
        )

    message_extra = await _build_message_extra_with_context(
        db,
        conversation_id=conversation_db_id,
        conversation_type=conversation_type,
        content=payload.content,
        extra=payload.extra or {},
    )

    user_message = ConversationMessage(
        conversation_id=conversation_db_id,
        user_id=user.id,
        role="user",
        content=payload.content,
        message_type=conversation_type,
        extra=payload.extra or {},
        ai_model_id=ai_model_db_id,
    )
    assistant_message = ConversationMessage(
        conversation_id=conversation_db_id,
        user_id=user.id,
        role="assistant",
        content="任务已提交，正在生成中",
        message_type=conversation_type,
        extra={"task_status": "pending"},
        ai_model_id=ai_model_db_id,
    )
    db.add(user_message)
    db.add(assistant_message)
    task_record = await create_user_task_record(
        db,
        user_id=user.id,
        ai_model_id=ai_model_db_id,
        points_transaction_id=points_transaction.id if points_transaction else None,
        business_type="conversation",
        business_id=conversation_db_id,
        generation_type=conversation_type,
        status="pending",
        title=conversation_title,
        prompt=payload.content,
        result=None,
        points_cost=ai_model_points_cost,
        extra={
            "conversation_id": str(conversation_db_id),
            "assistant_message_id": None,
            "user_message_extra": message_extra,
            "assistant_message_extra": {},
        },
    )
    await db.flush()
    assistant_message.extra = {
        **(assistant_message.extra or {}),
        "task_record_id": str(task_record.id),
    }
    task_record.extra = {
        **(task_record.extra or {}),
        "assistant_message_id": str(assistant_message.id),
    }
    await db.execute(
        Conversation.__table__.update()
        .where(Conversation.id == conversation_db_id)
        .values(updated_at=beijing_datetime())
    )
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    await db.refresh(user_message)
    await db.refresh(assistant_message)
    try:
        run_conversation_generation.delay(str(task_record.id), str(assistant_message.id))
    except Exception:
        await _mark_conversation_generation_enqueue_failed(
            db,
            task_record.id,
            assistant_message.id,
            user.id,
            ai_model_points_cost,
            conversation_title,
        )
        await db.refresh(assistant_message)
    return user_message, assistant_message, ai_model_points_cost


async def _build_message_extra_with_context(
    db: AsyncSession,
    conversation_id: UUID,
    conversation_type: str,
    content: str,
    extra: Dict[str, Any],
) -> Dict[str, Any]:
    if conversation_type == "video":
        return _build_video_message_extra(extra)
    if conversation_type != "text":
        return extra
    if extra.get("messages"):
        return extra

    chat_mode = str(extra.get("chat_mode") or extra.get("capability") or "chat")
    if chat_mode != "chat":
        return extra

    messages = await _build_text_context_messages(db, conversation_id, content, extra)
    return {**extra, "messages": messages}


def _build_video_message_extra(extra: Dict[str, Any]) -> Dict[str, Any]:
    generation_mode = _normalize_conversation_video_generation_mode(extra.get("generation_mode"))
    payload = dict(extra)
    payload["generation_mode"] = generation_mode
    payload["video_mode"] = CONVERSATION_VIDEO_MODE_TO_PROVIDER_MODE[generation_mode]
    payload["capability"] = CONVERSATION_VIDEO_MODE_TO_PROVIDER_MODE[generation_mode]

    if generation_mode == "reference":
        reference_images = _dedupe(
            _collect_extra_urls(payload, ("images", "image_urls", "uploaded_images", "reference_images"))
        )
        if reference_images:
            payload["images"] = reference_images
            payload["image_urls"] = reference_images
        payload.pop("first_frame_url", None)
        payload.pop("last_frame_url", None)
        return payload

    first_frame_url = _extract_media_url(payload.get("first_frame_url") or payload.get("first_frame"))
    last_frame_url = _extract_media_url(payload.get("last_frame_url") or payload.get("last_frame"))
    if not first_frame_url and not last_frame_url:
        raise AppException("首尾帧生成需要传入 first_frame_url 或 last_frame_url", code=40012, status_code=400)

    media_items = list(payload.get("media_items") or payload.get("media") or [])
    if first_frame_url:
        payload["first_frame_url"] = first_frame_url
        media_items.append({"type": "image_url", "image_url": {"url": first_frame_url}, "role": "first_frame"})
    if last_frame_url:
        payload["last_frame_url"] = last_frame_url
        media_items.append({"type": "image_url", "image_url": {"url": last_frame_url}, "role": "last_frame"})
    payload["media_items"] = media_items
    payload.pop("images", None)
    payload.pop("image_urls", None)
    payload.pop("uploaded_images", None)
    payload.pop("reference_images", None)
    return payload


def _normalize_conversation_video_generation_mode(value: Any) -> str:
    mode = str(value or "reference").strip()
    aliases = {
        "参考生成": "reference",
        "reference_generation": "reference",
        "image_to_video": "reference",
        "首尾帧生成": "first_last_frame",
        "first-last-frame": "first_last_frame",
        "first_last": "first_last_frame",
    }
    mode = aliases.get(mode, mode)
    if mode not in CONVERSATION_VIDEO_MODE_TO_PROVIDER_MODE:
        raise AppException("不支持的视频生成方式", code=40012, status_code=400)
    return mode


def _collect_extra_urls(extra: Dict[str, Any], keys: Tuple[str, ...]) -> List[str]:
    urls: List[str] = []
    for key in keys:
        for value in _as_list(extra.get(key)):
            url = _extract_media_url(value)
            if url:
                urls.append(url)
    return urls


def _dedupe(values: List[str]) -> List[str]:
    items: List[str] = []
    seen = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            items.append(value)
    return items


async def _build_text_context_messages(
    db: AsyncSession,
    conversation_id: UUID,
    current_content: str,
    current_extra: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    result = await db.execute(
        select(ConversationMessage)
        .where(
            ConversationMessage.conversation_id == conversation_id,
            ConversationMessage.message_type == "text",
            ConversationMessage.role.in_(("user", "assistant")),
        )
        .order_by(ConversationMessage.created_at.desc())
        .limit(DEFAULT_TEXT_CONTEXT_MESSAGE_LIMIT)
    )
    history = list(reversed(result.scalars().all()))
    messages: List[Dict[str, Any]] = []
    for item in history:
        if not item.content or item.content.startswith("任务已提交") or item.content.startswith("任务执行失败"):
            continue
        role = "assistant" if item.role == "assistant" else "user"
        messages.append({"role": role, "content": item.content})
    messages.append({"role": "user", "content": _build_text_multimodal_content(current_content, current_extra or {})})
    return messages


def _build_text_multimodal_content(content: str, extra: Dict[str, Any]) -> Any:
    media_urls = _collect_text_multimodal_media_urls(extra)
    if not media_urls:
        return content

    message_content: List[Dict[str, Any]] = [{"type": "text", "text": content}]
    for url in media_urls:
        message_content.append({"type": "image_url", "image_url": {"url": url}})
    return message_content


def _collect_text_multimodal_media_urls(extra: Dict[str, Any]) -> List[str]:
    values: List[Any] = []
    for key in TEXT_MULTIMODAL_MEDIA_KEYS:
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


def _extract_media_url(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("url", "image_url", "video_url", "file_url", "oss_url"):
            nested = value.get(key)
            if isinstance(nested, str):
                return nested
            if isinstance(nested, dict) and isinstance(nested.get("url"), str):
                return nested["url"]
    return ""


async def _mark_conversation_generation_enqueue_failed(
    db: AsyncSession,
    task_record_id: UUID,
    assistant_message_id: UUID,
    user_id: UUID,
    points_cost: int,
    title: str,
) -> None:
    assistant_message = await db.get(ConversationMessage, assistant_message_id)
    record = await db.get(UserTaskRecord, task_record_id)
    refund_transaction_id = None
    if points_cost > 0:
        refund_transaction = await change_user_points(
            db,
            user_id=user_id,
            amount=points_cost,
            transaction_type="refund",
            remark=f"任务入队失败退回积分：{title}",
            auto_commit=False,
        )
        refund_transaction_id = str(refund_transaction.id)
    if record:
        record.status = "failed"
        record.result = "任务入队失败"
        record.extra = {
            **(record.extra or {}),
            "failed_reason": "任务入队失败",
            "refund_transaction_id": refund_transaction_id,
        }
    if assistant_message:
        assistant_message.content = "任务入队失败，请稍后重试"
        assistant_message.extra = {
            **(assistant_message.extra or {}),
            "task_status": "failed",
            "failed_reason": "任务入队失败",
        }
    await db.commit()


async def query_conversation_generation_task(
    db: AsyncSession,
    conversation_id: UUID,
    user: User,
    task_id: str,
) -> ModelRunResult:
    conversation = await get_conversation_or_404(db, conversation_id, user.id)
    if conversation.conversation_type not in {"image", "video"}:
        raise AppException("该会话类型没有生成任务查询接口", code=40006, status_code=400)

    ai_model = await get_enabled_conversation_model_or_404(
        db,
        conversation.ai_model_id,
        conversation.conversation_type,
    )
    result = await query_model_task(ai_model, conversation.conversation_type, task_id)
    return await persist_generated_media_to_oss(conversation.conversation_type, result)
