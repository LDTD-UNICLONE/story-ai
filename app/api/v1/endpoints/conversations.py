import asyncio
import json
import time
from typing import Any, AsyncIterator, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.public_messages import sanitize_public_message
from app.core.responses import success
from app.db.session import AsyncSessionLocal, get_db
from app.models.conversation import ConversationMessage
from app.models.task_record import UserTaskRecord
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
    retry_text_conversation_turn,
    send_conversation_message,
    update_conversation,
)
from app.services.task_records import task_record_progress_percent

router = APIRouter(prefix="/conversations")
SSE_POLL_INTERVAL_SECONDS = 0.5
SSE_HEARTBEAT_SECONDS = 15
SSE_MAX_CONNECTION_SECONDS = max(
    60,
    settings.effective_celery_task_time_limit_seconds + 30,
)


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
    return success(
        data=ConversationOut.model_validate(conversation).model_dump(mode="json"),
        message="创建成功",
    )


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
    return success(
        data=ConversationOut.model_validate(conversation).model_dump(mode="json"),
        message="删除成功",
    )


@router.patch("/{conversation_id}")
async def update_my_conversation(
    conversation_id: UUID,
    payload: ConversationUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conversation = await update_conversation(db, conversation_id, current_user.id, payload)
    return success(
        data=ConversationOut.model_validate(conversation).model_dump(mode="json"),
        message="更新成功",
    )


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
    return success(
        data=ConversationMessageOut.model_validate(message).model_dump(mode="json"),
        message="删除成功",
    )


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
        assistant_message=ConversationMessageOut.model_validate(assistant_message)
        if assistant_message
        else None,
        stop_polling=task_record.status in {"success", "failed"},
        next_poll_seconds=next_poll_seconds,
        progress_percent=task_record_progress_percent(task_record),
        created_at=task_record.created_at,
        updated_at=task_record.updated_at,
    )
    if next_poll_seconds:
        response.headers["X-Next-Poll-Seconds"] = str(next_poll_seconds)
    return success(data=data.model_dump(mode="json"))


@router.get("/{conversation_id}/generation-tasks/{task_record_id}/stream")
async def stream_my_conversation_generation_task(
    conversation_id: UUID,
    task_record_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task_record, assistant_message = await get_conversation_generation_task_status(
        db,
        conversation_id=conversation_id,
        user_id=current_user.id,
        task_record_id=task_record_id,
    )
    assistant_message_id = assistant_message.id if assistant_message is not None else None
    await db.rollback()
    return StreamingResponse(
        _conversation_generation_event_stream(
            request,
            task_record_id=task_record.id,
            assistant_message_id=assistant_message_id,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store",
            "X-Accel-Buffering": "no",
        },
    )


async def _conversation_generation_event_stream(
    request: Request,
    *,
    task_record_id: UUID,
    assistant_message_id: Optional[UUID],
) -> AsyncIterator[str]:
    sent_content = ""
    last_status: Optional[str] = None
    last_progress_percent: Optional[int] = None
    progress_sent = False
    last_emit_at = time.monotonic()
    connected_at = last_emit_at

    while not await request.is_disconnected():
        if time.monotonic() - connected_at >= SSE_MAX_CONNECTION_SECONDS:
            yield _sse_event(
                "reconnect",
                {"status": last_status or "running", "stop_streaming": False},
            )
            return
        async with AsyncSessionLocal() as stream_db:
            task_record = await stream_db.get(UserTaskRecord, task_record_id)
            assistant_message = (
                await stream_db.get(ConversationMessage, assistant_message_id)
                if assistant_message_id is not None
                else None
            )

        if task_record is None:
            yield _sse_event(
                "failed",
                {"status": "failed", "message": "任务记录不存在", "stop_streaming": True},
            )
            return

        status = str(task_record.status or "pending")
        stream_started = bool(
            assistant_message is not None and (assistant_message.extra or {}).get("stream_started")
        )
        raw_content = conversation_task_content(task_record, assistant_message) or ""
        content = sanitize_public_message(raw_content, fallback=raw_content)
        progress_percent = task_record_progress_percent(task_record)

        if status != last_status:
            yield _sse_event(
                "status",
                {
                    "task_record_id": str(task_record.id),
                    "assistant_message_id": (
                        str(assistant_message_id) if assistant_message_id else None
                    ),
                    "status": status,
                },
            )
            last_status = status
            last_emit_at = time.monotonic()

        if progress_percent is not None and (
            not progress_sent or progress_percent != last_progress_percent
        ):
            yield _sse_event(
                "progress",
                {
                    "task_record_id": str(task_record.id),
                    "assistant_message_id": (
                        str(assistant_message_id) if assistant_message_id else None
                    ),
                    "status": status,
                    "progress_percent": progress_percent,
                },
            )
            last_progress_percent = progress_percent
            progress_sent = True
            last_emit_at = time.monotonic()

        if stream_started and content != sent_content:
            if sent_content and content.startswith(sent_content):
                delta = content[len(sent_content) :]
                event_name = "delta"
                event_data = {"delta": delta, "content": content, "status": status}
            else:
                event_name = "snapshot"
                event_data = {"content": content, "status": status}
            sent_content = content
            yield _sse_event(event_name, event_data)
            last_emit_at = time.monotonic()

        if status in {"success", "failed"}:
            yield _sse_event(
                "completed" if status == "success" else "failed",
                {
                    "task_record_id": str(task_record.id),
                    "assistant_message_id": (
                        str(assistant_message_id) if assistant_message_id else None
                    ),
                    "status": status,
                    "progress_percent": progress_percent,
                    "content": content,
                    "stop_streaming": True,
                },
            )
            return

        if time.monotonic() - last_emit_at >= SSE_HEARTBEAT_SECONDS:
            yield ": keep-alive\n\n"
            last_emit_at = time.monotonic()
        await asyncio.sleep(SSE_POLL_INTERVAL_SECONDS)


def _sse_event(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


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


@router.post("/{conversation_id}/messages/{user_message_id}/retry")
async def retry_my_text_conversation_turn(
    conversation_id: UUID,
    user_message_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    user_message, assistant_message, points_cost = await retry_text_conversation_turn(
        db,
        conversation_id=conversation_id,
        user_message_id=user_message_id,
        user=current_user,
    )
    data = ConversationSendMessageOut(
        user_message=ConversationMessageOut.model_validate(user_message),
        assistant_message=ConversationMessageOut.model_validate(assistant_message),
        points_cost=points_cost,
        task_record_id=_parse_uuid((assistant_message.extra or {}).get("task_record_id")),
        task_status=(assistant_message.extra or {}).get("task_status") or "pending",
    )
    return success(data=data.model_dump(mode="json"), message="重试已提交")


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
