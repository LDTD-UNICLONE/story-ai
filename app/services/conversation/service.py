from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import beijing_datetime
from app.core.exceptions import AppException
from app.integrations import apimart
from app.models.ai_model import AiModel
from app.models.conversation import Conversation, ConversationMessage
from app.models.task_record import UserTaskRecord
from app.models.user import User
from app.schemas.conversation import (
    ConversationCreateRequest, ConversationSendMessageRequest,
    ConversationUpdateRequest,
)
from app.services.generation.task_dispatch import dispatch_tasks_best_effort
from app.services.generation.runner import ModelRunResult, validate_model_request
from app.services.billing.model_points import (
    calculate_submission_points_cost,
    ensure_model_minimum_balance,
)
from app.services.generation.submission import pending_generation
from app.services.generation.task_records import (
    expire_stale_task_record,
    expire_stale_task_records,
    interrupt_task_record,
)
from app.services.conversation.text_context import build_text_context_messages
from app.services.conversation.video_inputs import build_video_message_extra
from app.services.conversation.image_references import compile_image_references
from app.services.seedance_images import resolve_image_ids, reviewed_video_extra


SUPPORTED_CONVERSATION_TYPES = {"text", "image", "video"}


async def get_enabled_conversation_model_or_404(
    db: AsyncSession,
    ai_model_id: UUID,
    conversation_type: Optional[str] = None,
) -> AiModel:
    if conversation_type is not None and conversation_type not in SUPPORTED_CONVERSATION_TYPES:
        raise AppException("不支持的对话类型", code=40004, status_code=400)

    result = await db.execute(
        select(AiModel).where(
            AiModel.id == ai_model_id,
            AiModel.is_enabled.is_(True),
            AiModel.model_type.in_(
                [conversation_type] if conversation_type else SUPPORTED_CONVERSATION_TYPES
            ),
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
    await expire_stale_task_records(db, user_id=user_id, business_type="conversation")

    count_result = await db.execute(
        select(func.count())
        .select_from(Conversation)
        .where(Conversation.user_id == user_id, Conversation.is_enabled.is_(True))
    )
    total = count_result.scalar_one()

    result = await db.execute(
        select(Conversation)
        .where(Conversation.user_id == user_id, Conversation.is_enabled.is_(True))
        .order_by(Conversation.created_at.desc(), Conversation.id.desc())
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
    conversation = await _lock_conversation(db, conversation_id, user_id)
    conversation.is_enabled = False
    conversation.updated_at = beijing_datetime()
    await db.flush()
    active_task_result = await db.execute(
        select(UserTaskRecord.id).where(
            UserTaskRecord.user_id == user_id,
            UserTaskRecord.business_type == "conversation",
            UserTaskRecord.business_id == conversation_id,
            UserTaskRecord.status.in_(("pending", "running")),
        )
    )
    for task_record_id in active_task_result.scalars().all():
        try:
            await interrupt_task_record(
                db,
                task_record_id,
                user_id,
                reason="用户删除会话，任务已取消",
            )
        except AppException as exc:
            if exc.code not in {40035, 40406}:
                raise
    await db.commit()
    await db.refresh(conversation)
    return conversation


async def delete_conversation_message(
    db: AsyncSession,
    conversation_id: UUID,
    message_id: UUID,
    user_id: UUID,
) -> ConversationMessage:
    await get_conversation_or_404(db, conversation_id, user_id)
    result = await db.execute(
        select(ConversationMessage).where(
            ConversationMessage.id == message_id,
            ConversationMessage.conversation_id == conversation_id,
            ConversationMessage.user_id == user_id,
        )
    )
    message = result.scalar_one_or_none()
    if message is None:
        raise AppException("消息记录不存在", code=40407, status_code=404)

    await _interrupt_message_task_if_active(db, message, user_id)
    await db.delete(message)
    await db.execute(
        Conversation.__table__.update()
        .where(Conversation.id == conversation_id)
        .values(updated_at=beijing_datetime())
    )
    await db.commit()
    return message


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


async def _interrupt_message_task_if_active(
    db: AsyncSession,
    message: ConversationMessage,
    user_id: UUID,
) -> None:
    direct_task_record_id = _parse_uuid((message.extra or {}).get("task_record_id"))
    result = await db.execute(
        select(UserTaskRecord).where(
            UserTaskRecord.user_id == user_id,
            UserTaskRecord.business_type == "conversation",
            UserTaskRecord.business_id == message.conversation_id,
            UserTaskRecord.status.in_(("pending", "running")),
        )
    )
    for record in result.scalars().all():
        record_extra = record.extra or {}
        is_linked = record.id == direct_task_record_id or str(message.id) in {
            str(record_extra.get("user_message_id") or ""),
            str(record_extra.get("assistant_message_id") or ""),
        }
        if is_linked:
            await interrupt_task_record(
                db,
                record.id,
                user_id,
                reason="用户删除对话历史，任务已取消",
            )


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

    order_by = (
        (
            ConversationMessage.sequence_no.desc().nullslast(),
            ConversationMessage.created_at.desc(),
            ConversationMessage.id.desc(),
        )
        if order == "desc"
        else (
            ConversationMessage.sequence_no.asc().nullsfirst(),
            ConversationMessage.created_at.asc(),
            ConversationMessage.id.asc(),
        )
    )
    result = await db.execute(
        select(ConversationMessage)
        .where(ConversationMessage.conversation_id == conversation_id)
        .order_by(*order_by)
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
    await db.refresh(task_record)

    assistant_message_id = (task_record.extra or {}).get("assistant_message_id")
    assistant_message = None
    parsed_assistant_message_id = _parse_uuid(assistant_message_id)
    if parsed_assistant_message_id:
        assistant_result = await db.execute(
            select(ConversationMessage).where(
                ConversationMessage.id == parsed_assistant_message_id,
                ConversationMessage.conversation_id == conversation_id,
                ConversationMessage.user_id == user_id,
                ConversationMessage.role == "assistant",
            )
        )
        assistant_message = assistant_result.scalar_one_or_none()

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
    status_is_synced = (
        assistant_message.message_type != "text" or assistant_message.status == expected_status
    )
    if current_status == expected_status and status_is_synced and not should_sync_content:
        return

    assistant_message.extra = {
        **(assistant_message.extra or {}),
        **((task_record.extra or {}).get("assistant_message_extra") or {}),
        "task_status": expected_status,
        "task_record_id": str(task_record.id),
    }
    if task_record.status == "failed":
        failed_content = _terminal_assistant_content(task_record)
        assistant_message.extra = {
            **(assistant_message.extra or {}),
            "failed_reason": task_record.result,
            "display_message": failed_content,
        }
    if expected_content:
        assistant_message.content = expected_content
    if assistant_message.message_type == "text":
        assistant_message.status = expected_status
    await db.commit()
    await db.refresh(task_record)
    await db.refresh(assistant_message)


async def send_conversation_message(
    db: AsyncSession,
    conversation_id: UUID,
    user: User,
    payload: ConversationSendMessageRequest,
) -> Tuple[ConversationMessage, ConversationMessage, int]:
    await get_conversation_or_404(db, conversation_id, user.id)
    await expire_stale_task_records(
        db, user_id=user.id, business_type="conversation",
    )
    conversation = await _lock_conversation(db, conversation_id, user.id)
    existing_submission = await _find_idempotent_text_submission(db, conversation, payload)
    if existing_submission is not None:
        return existing_submission

    selected_ai_model_id = payload.ai_model_id or conversation.ai_model_id
    ai_model = await get_enabled_conversation_model_or_404(
        db,
        selected_ai_model_id,
    )
    if ai_model.model_type == "text":
        await _ensure_no_active_text_generation(db, conversation)
    conversation_db_id = conversation.id
    conversation_title = conversation.title
    conversation_type = ai_model.model_type
    ai_model_db_id = ai_model.id
    ai_model_nickname = ai_model.nickname

    resolved_urls = await resolve_image_ids(db, user.id, payload.image_references)
    model_prompt, reference_extra = compile_image_references(
        payload.content, payload.extra or {}, payload.image_references, conversation_type,
        resolved_urls=resolved_urls,
    )
    message_extra = await _build_message_extra_with_context(
        db,
        conversation_id=conversation_db_id,
        conversation_type=conversation_type,
        content=model_prompt,
        extra=reference_extra,
        ai_model=ai_model,
    )
    if (
        payload.image_references
        and conversation_type == "video"
        and message_extra.get("generation_mode") != "reference"
    ):
        raise AppException("图片引用需要使用视频参考生成模式", code=40016)
    message_extra = await reviewed_video_extra(db, user.id, ai_model, model_prompt, message_extra)
    validate_model_request(
        ai_model, conversation_type, model_prompt, message_extra
    )
    await ensure_model_minimum_balance(db, user.id, ai_model)
    ai_model_points_cost = calculate_submission_points_cost(
        ai_model, conversation_type, message_extra
    )

    turn_id = uuid4() if conversation_type == "text" else None
    first_sequence_no = await _next_message_sequence_no(db, conversation_db_id)
    user_message = ConversationMessage(
        conversation_id=conversation_db_id,
        user_id=user.id,
        role="user",
        content=payload.content,
        message_type=conversation_type,
        extra={
            **(payload.extra or {}),
            **({
                "image_references": [
                    {**item.model_dump(mode="json", exclude_none=True),
                     **({"url": resolved_urls[item.name]} if item.image_id else {})}
                    for item in payload.image_references
                ],
            } if payload.image_references else {}),
        },
        ai_model_id=ai_model_db_id,
        turn_id=turn_id,
        sequence_no=first_sequence_no,
        status="success" if conversation_type == "text" else None,
        client_message_id=(payload.client_message_id if conversation_type == "text" else None),
    )
    assistant_message = ConversationMessage(
        id=uuid4(),
        conversation_id=conversation_db_id,
        user_id=user.id,
        role="assistant",
        content="任务已提交，正在生成中",
        message_type=conversation_type,
        extra={"task_status": "pending"},
        ai_model_id=ai_model_db_id,
        turn_id=turn_id,
        sequence_no=first_sequence_no + 1,
        status="pending" if conversation_type == "text" else None,
    )
    async with pending_generation(
        db,
        user_id=user.id,
        ai_model_id=ai_model_db_id,
        business_type="conversation",
        business_id=conversation_db_id,
        generation_type=conversation_type,
        title=conversation_title,
        prompt=model_prompt,
        points_cost=ai_model_points_cost,
        charge_remark=f"对话模型调用：{ai_model_nickname}",
        extra={
            "conversation_id": str(conversation_db_id),
            "turn_id": str(turn_id) if turn_id else None,
            "user_message_id": None,
            "assistant_message_id": None,
            "submission_points_cost": ai_model_points_cost,
            "user_message_extra": message_extra,
            "assistant_message_extra": {},
        },
        expire_stale=conversation_type != "text",
        task_name="tasks.model_generation.run_conversation_generation",
        task_args=(str(assistant_message.id),),
    ) as task_record:
        db.add(user_message)
        db.add(assistant_message)
        conversation.ai_model_id = ai_model_db_id
        conversation.conversation_type = conversation_type
        await db.flush()
        assistant_message.extra = {
            **(assistant_message.extra or {}),
            "task_record_id": str(task_record.id),
        }
        task_record.extra = {
            **(task_record.extra or {}),
            "user_message_id": str(user_message.id),
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
    await dispatch_tasks_best_effort(db, [task_record.id])
    return user_message, assistant_message, ai_model_points_cost


async def _lock_conversation(
    db: AsyncSession,
    conversation_id: UUID,
    user_id: UUID,
) -> Conversation:
    result = await db.execute(
        select(Conversation)
        .where(
            Conversation.id == conversation_id,
            Conversation.user_id == user_id,
            Conversation.is_enabled.is_(True),
        )
        .with_for_update()
    )
    conversation = result.scalar_one_or_none()
    if conversation is None:
        raise AppException("会话不存在", code=40405, status_code=404)
    return conversation


async def _find_idempotent_text_submission(
    db: AsyncSession,
    conversation: Conversation,
    payload: ConversationSendMessageRequest,
) -> Optional[Tuple[ConversationMessage, ConversationMessage, int]]:
    if not payload.client_message_id:
        return None

    result = await db.execute(
        select(ConversationMessage).where(
            ConversationMessage.conversation_id == conversation.id,
            ConversationMessage.role == "user",
            ConversationMessage.message_type == "text",
            ConversationMessage.client_message_id == payload.client_message_id,
        )
    )
    user_message = result.scalar_one_or_none()
    if user_message is None:
        return None

    model_mismatch = (
        payload.ai_model_id is not None and user_message.ai_model_id != payload.ai_model_id
    )
    if (
        user_message.content != payload.content
        or model_mismatch
        or (user_message.extra or {}) != (payload.extra or {})
        or payload.image_references
    ):
        raise AppException(
            "client_message_id 已用于其他文本消息",
            code=40997,
            status_code=409,
        )

    assistant_result = await db.execute(
        select(ConversationMessage)
        .where(
            ConversationMessage.conversation_id == conversation.id,
            ConversationMessage.user_id == conversation.user_id,
            ConversationMessage.turn_id == user_message.turn_id,
            ConversationMessage.role == "assistant",
            ConversationMessage.message_type == "text",
        )
        .order_by(
            ConversationMessage.sequence_no.desc().nullslast(),
            ConversationMessage.created_at.desc(),
        )
        .limit(1)
    )
    assistant_message = assistant_result.scalar_one_or_none()
    if assistant_message is None:
        raise AppException(
            "幂等文本消息缺少对应回答记录",
            code=40998,
            status_code=409,
        )

    task_record_id = _parse_uuid((assistant_message.extra or {}).get("task_record_id"))
    task_record = None
    if task_record_id is not None:
        task_result = await db.execute(
            select(UserTaskRecord).where(
                UserTaskRecord.id == task_record_id,
                UserTaskRecord.user_id == conversation.user_id,
                UserTaskRecord.business_type == "conversation",
                UserTaskRecord.business_id == conversation.id,
            )
        )
        task_record = task_result.scalar_one_or_none()
    points_cost = (
        int((task_record.extra or {}).get("submission_points_cost", task_record.points_cost))
        if task_record
        else 0
    )
    return user_message, assistant_message, points_cost


async def _ensure_no_active_text_generation(
    db: AsyncSession,
    conversation: Conversation,
) -> None:
    result = await db.execute(
        select(UserTaskRecord)
        .where(
            UserTaskRecord.business_type == "conversation",
            UserTaskRecord.business_id == conversation.id,
            UserTaskRecord.generation_type == "text",
            UserTaskRecord.status.in_(("pending", "running")),
        )
        .order_by(UserTaskRecord.created_at.desc())
        .limit(1)
    )
    active_task = result.scalar_one_or_none()
    if active_task is None:
        return
    raise AppException(
        "当前文本会话仍有消息正在生成，请等待完成后再发送",
        code=40996,
        status_code=409,
        data={
            "active_task_record_id": str(active_task.id),
            "assistant_message_id": (active_task.extra or {}).get("assistant_message_id"),
        },
    )


async def _next_message_sequence_no(db: AsyncSession, conversation_id: UUID) -> int:
    result = await db.execute(
        select(func.max(ConversationMessage.sequence_no)).where(
            ConversationMessage.conversation_id == conversation_id,
        )
    )
    return int(result.scalar_one() or 0) + 1


async def retry_text_conversation_turn(
    db: AsyncSession,
    conversation_id: UUID,
    user_message_id: UUID,
    user: User,
) -> Tuple[ConversationMessage, ConversationMessage, int]:
    await get_conversation_or_404(db, conversation_id, user.id)

    await expire_stale_task_records(
        db,
        user_id=user.id,
        business_type="conversation",
        generation_type="text",
    )
    conversation = await _lock_conversation(db, conversation_id, user.id)

    result = await db.execute(
        select(ConversationMessage).where(
            ConversationMessage.id == user_message_id,
            ConversationMessage.conversation_id == conversation.id,
            ConversationMessage.user_id == user.id,
            ConversationMessage.role == "user",
        )
    )
    user_message = result.scalar_one_or_none()
    if user_message is None:
        raise AppException("文本用户消息不存在", code=40407, status_code=404)
    if user_message.message_type != "text":
        raise AppException("只有文本消息支持失败重试", code=40014, status_code=400)
    if user_message.turn_id is None:
        raise AppException("旧版文本消息不支持按轮次重试", code=40999, status_code=409)
    await _ensure_no_active_text_generation(db, conversation)

    assistant_result = await db.execute(
        select(ConversationMessage)
        .where(
            ConversationMessage.conversation_id == conversation.id,
            ConversationMessage.turn_id == user_message.turn_id,
            ConversationMessage.role == "assistant",
            ConversationMessage.message_type == "text",
        )
        .order_by(
            ConversationMessage.sequence_no.desc().nullslast(),
            ConversationMessage.created_at.desc(),
        )
        .limit(1)
    )
    previous_assistant = assistant_result.scalar_one_or_none()
    previous_status = (
        previous_assistant.status
        if previous_assistant is not None
        else None
    ) or ((previous_assistant.extra or {}).get("task_status") if previous_assistant else None)
    if previous_assistant is None or previous_status != "failed":
        raise AppException("只有生成失败的文本轮次可以重试", code=40995, status_code=409)

    ai_model = await get_enabled_conversation_model_or_404(
        db,
        user_message.ai_model_id,
        "text",
    )
    raw_extra = dict(user_message.extra or {})
    raw_extra.pop("messages", None)
    message_extra = await _build_message_extra_with_context(
        db,
        conversation_id=conversation.id,
        conversation_type="text",
        content=user_message.content,
        extra=raw_extra,
        ai_model=ai_model,
        before_sequence_no=user_message.sequence_no,
    )
    validate_model_request(ai_model, "text", user_message.content, message_extra)
    await ensure_model_minimum_balance(db, user.id, ai_model)
    points_cost = calculate_submission_points_cost(ai_model, "text", message_extra)
    assistant_message = ConversationMessage(
        id=uuid4(),
        conversation_id=conversation.id,
        user_id=user.id,
        role="assistant",
        content="任务已提交，正在重新生成中",
        message_type="text",
        extra={
            "task_status": "pending",
            "retry_of_assistant_message_id": str(previous_assistant.id),
        },
        ai_model_id=ai_model.id,
        turn_id=user_message.turn_id,
        sequence_no=await _next_message_sequence_no(db, conversation.id),
        status="pending",
    )
    async with pending_generation(
        db,
        user_id=user.id,
        ai_model_id=ai_model.id,
        business_type="conversation",
        business_id=conversation.id,
        generation_type="text",
        title=conversation.title,
        prompt=user_message.content,
        points_cost=points_cost,
        charge_remark=f"对话模型调用：{ai_model.nickname}",
        extra={
            "conversation_id": str(conversation.id),
            "turn_id": str(user_message.turn_id),
            "user_message_id": str(user_message.id),
            "assistant_message_id": None,
            "retry_of_assistant_message_id": str(previous_assistant.id),
            "submission_points_cost": points_cost,
            "user_message_extra": message_extra,
            "assistant_message_extra": {},
        },
        task_name="tasks.model_generation.run_conversation_generation",
        task_args=(str(assistant_message.id),),
    ) as task_record:
        db.add(assistant_message)
        await db.flush()
        assistant_message.extra = {
            **(assistant_message.extra or {}),
            "task_record_id": str(task_record.id),
        }
        task_record.extra = {
            **(task_record.extra or {}),
            "assistant_message_id": str(assistant_message.id),
        }
        conversation.updated_at = beijing_datetime()
    await db.commit()
    await db.refresh(user_message)
    await db.refresh(assistant_message)

    await dispatch_tasks_best_effort(db, [task_record.id])
    return user_message, assistant_message, points_cost


async def _build_message_extra_with_context(
    db: AsyncSession,
    conversation_id: UUID,
    conversation_type: str,
    content: str,
    extra: Dict[str, Any],
    ai_model: Optional[AiModel] = None,
    *,
    before_sequence_no: Optional[int] = None,
) -> Dict[str, Any]:
    if conversation_type == "video":
        return await build_video_message_extra(extra, ai_model)
    if conversation_type != "text":
        return extra
    if "messages" in extra:
        raise AppException(
            "文本对话历史消息由后端维护，不能提交 extra.messages",
            code=40013,
            status_code=400,
        )

    chat_mode = str(extra.get("chat_mode") or extra.get("capability") or "chat")
    is_apimart_text = ai_model is not None and ai_model.vendor == apimart.APIMART_VENDOR
    if chat_mode != "chat" and not is_apimart_text:
        return extra

    messages = await build_text_context_messages(
        db, conversation_id, content, extra, before_sequence_no=before_sequence_no
    )
    return {**extra, "messages": messages}


async def query_conversation_generation_task(
    db: AsyncSession,
    conversation_id: UUID,
    user: User,
    task_id: str,
) -> ModelRunResult:
    await get_conversation_or_404(db, conversation_id, user.id)

    result = await db.execute(
        select(UserTaskRecord)
        .where(
            UserTaskRecord.user_id == user.id,
            UserTaskRecord.business_type == "conversation",
            UserTaskRecord.business_id == conversation_id,
            UserTaskRecord.generation_type.in_(("image", "video")),
        )
        .order_by(UserTaskRecord.created_at.desc())
        .limit(100)
    )
    for record in result.scalars().all():
        if _task_record_provider_task_id(record) == str(task_id):
            return ModelRunResult(
                content=record.result or "",
                extra={
                    "task_id": str(task_id),
                    "task_status": record.status,
                    "task_record_id": str(record.id),
                    "record_extra": record.extra or {},
                },
            )
    raise AppException("任务记录不存在", code=40406, status_code=404)


def _task_record_provider_task_id(record: UserTaskRecord) -> Optional[str]:
    extra = record.extra or {}
    candidates = [
        extra.get("task_id"),
        extra.get("provider_task_id"),
        (extra.get("model_result_extra") or {}).get("task_id"),
        (extra.get("assistant_message_extra") or {}).get("task_id"),
        (extra.get("last_provider_task_status") or {}).get("task_id"),
        (extra.get("last_provider_task_status") or {}).get("taskId"),
    ]
    for value in candidates:
        if value:
            return str(value)
    return None
