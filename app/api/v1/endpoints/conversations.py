from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.responses import success
from app.db.session import get_db
from app.models.user import User
from app.schemas.conversation import (
    ConversationCreateRequest,
    ConversationGenerationTaskOut,
    ConversationListOut,
    ConversationMessageListOut,
    ConversationMessageOut,
    ConversationOut,
    ConversationSendMessageOut,
    ConversationSendMessageRequest,
    ConversationTaskStatusOut,
    ConversationUpdateRequest,
)
from app.services.conversations import (
    conversation_task_content,
    create_conversation,
    delete_conversation,
    delete_conversation_message,
    get_conversation_generation_task_status,
    get_conversation_or_404,
    list_conversation_messages,
    list_conversations,
    query_conversation_generation_task,
    send_conversation_message,
    update_conversation,
)

router = APIRouter(prefix="/conversations")


@router.get("")
async def my_conversations(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conversations, total = await list_conversations(
        db,
        user_id=current_user.id,
        page=page,
        page_size=page_size,
    )
    data = ConversationListOut(
        items=[ConversationOut.model_validate(item) for item in conversations],
        total=total,
        page=page,
        page_size=page_size,
    )
    return success(data=data.model_dump(mode="json"))


@router.post("")
async def create_my_conversation(
    payload: ConversationCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conversation = await create_conversation(db, current_user, payload)
    return success(data=ConversationOut.model_validate(conversation).model_dump(mode="json"), message="创建成功")


@router.get("/{conversation_id}")
async def my_conversation_detail(
    conversation_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conversation = await get_conversation_or_404(db, conversation_id, current_user.id)
    return success(data=ConversationOut.model_validate(conversation).model_dump(mode="json"))


@router.delete("/{conversation_id}")
async def delete_my_conversation(
    conversation_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conversation = await delete_conversation(db, conversation_id, current_user.id)
    return success(data=ConversationOut.model_validate(conversation).model_dump(mode="json"), message="删除成功")


@router.patch("/{conversation_id}")
async def update_my_conversation(
    conversation_id: UUID,
    payload: ConversationUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conversation = await update_conversation(db, conversation_id, current_user.id, payload)
    return success(data=ConversationOut.model_validate(conversation).model_dump(mode="json"), message="更新成功")


@router.get("/{conversation_id}/messages")
async def my_conversation_messages(
    conversation_id: UUID,
    response: Response,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    response.headers["Cache-Control"] = "no-store"
    messages, total = await list_conversation_messages(
        db,
        conversation_id=conversation_id,
        user_id=current_user.id,
        page=page,
        page_size=page_size,
        order=order,
    )
    data = ConversationMessageListOut(
        items=[ConversationMessageOut.model_validate(item) for item in messages],
        total=total,
        page=page,
        page_size=page_size,
    )
    return success(data=data.model_dump(mode="json"))


@router.delete("/{conversation_id}/messages/{message_id}")
async def delete_my_conversation_message(
    conversation_id: UUID,
    message_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    message = await delete_conversation_message(
        db,
        conversation_id=conversation_id,
        message_id=message_id,
        user_id=current_user.id,
    )
    return success(data=ConversationMessageOut.model_validate(message).model_dump(mode="json"), message="删除成功")


@router.get("/{conversation_id}/generation-tasks/{task_record_id}")
async def my_conversation_generation_task(
    conversation_id: UUID,
    task_record_id: UUID,
    response: Response,
    wait_seconds: int = Query(default=0, ge=0, le=15),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    response.headers["Cache-Control"] = "no-store"
    task_record, assistant_message = await get_conversation_generation_task_status(
        db,
        conversation_id=conversation_id,
        user_id=current_user.id,
        task_record_id=task_record_id,
    )
    assistant_message_id = (task_record.extra or {}).get("assistant_message_id")
    next_poll_seconds = (task_record.extra or {}).get("next_poll_seconds")
    if task_record.status in {"pending", "running"} and next_poll_seconds is None:
        next_poll_seconds = max(1, wait_seconds or 3)
    content = conversation_task_content(task_record, assistant_message)
    failed_reason = task_record.result if task_record.status == "failed" else None
    data = ConversationGenerationTaskOut(
        task_record_id=task_record.id,
        conversation_id=conversation_id,
        assistant_message_id=_parse_uuid(assistant_message_id),
        status=task_record.status,
        task_status=task_record.status,
        content=content,
        result=task_record.result,
        message=content,
        failed_reason=failed_reason,
        extra=task_record.extra or {},
        assistant_message=ConversationMessageOut.model_validate(assistant_message) if assistant_message else None,
        stop_polling=task_record.status in {"success", "failed"},
        next_poll_seconds=next_poll_seconds,
        created_at=task_record.created_at,
        updated_at=task_record.updated_at,
    )
    if next_poll_seconds:
        response.headers["X-Next-Poll-Seconds"] = str(next_poll_seconds)
    return success(data=data.model_dump(mode="json"))


def _parse_uuid(value: Any) -> Optional[UUID]:
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


@router.post("/{conversation_id}/messages")
async def send_my_conversation_message(
    conversation_id: UUID,
    payload: ConversationSendMessageRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    user_message, assistant_message, points_cost = await send_conversation_message(
        db,
        conversation_id=conversation_id,
        user=current_user,
        payload=payload,
    )
    data = ConversationSendMessageOut(
        user_message=ConversationMessageOut.model_validate(user_message),
        assistant_message=ConversationMessageOut.model_validate(assistant_message),
        points_cost=points_cost,
        task_record_id=_parse_uuid((assistant_message.extra or {}).get("task_record_id")),
        task_status=(assistant_message.extra or {}).get("task_status") or "pending",
    )
    return success(data=data.model_dump(mode="json"), message="发送成功")


@router.get("/{conversation_id}/tasks/{task_id}")
async def my_conversation_task_status(
    conversation_id: UUID,
    task_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await query_conversation_generation_task(
        db,
        conversation_id=conversation_id,
        user=current_user,
        task_id=task_id,
    )
    data = ConversationTaskStatusOut(
        task_id=str(result.extra.get("task_id") or task_id),
        task_status=result.extra.get("task_status"),
        content=result.content,
        extra=result.extra,
    )
    return success(data=data.model_dump(mode="json"))
