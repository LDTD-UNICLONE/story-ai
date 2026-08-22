from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import beijing_datetime
from app.core.exceptions import AppException
from app.integrations import apimart, comfly
from app.integrations.apimart_video_specs import (
    merge_video_capabilities as merge_apimart_video_capabilities,
    normalize_video_resolution as normalize_apimart_video_resolution,
)
from app.integrations.comfly_video_specs import (
    merge_video_capabilities as merge_comfly_video_capabilities,
)
from app.integrations.volcengine_ark_video_specs import (
    is_known_video_resolution,
    is_video_resolution_supported,
    is_volcengine_ark_video_model,
    merge_video_capabilities as merge_ark_video_capabilities,
    normalize_video_resolution,
)
from app.models.ai_model import AiModel
from app.models.conversation import Conversation, ConversationMessage
from app.models.task_record import UserTaskRecord
from app.models.user import User
from app.schemas.conversation import (
    ConversationCreateRequest, ConversationSendMessageRequest,
    ConversationUpdateRequest,
)
from app.services.model_runner import ModelRunResult
from app.services.model_configuration import model_request_capabilities
from app.services.model_points import (
    calculate_submission_points_cost,
    ensure_model_minimum_balance,
)
from app.services.points import change_user_points, consume_user_points
from app.services.task_records import (
    create_user_task_record,
    expire_stale_task_record,
    expire_stale_task_records,
    interrupt_task_record,
)
from app.services.uploads import probe_media_url
from app.tasks.model_generation import run_conversation_generation


SUPPORTED_CONVERSATION_TYPES = {"text", "image", "video"}
DEFAULT_TEXT_CONTEXT_MESSAGE_LIMIT = 20
DEFAULT_TEXT_CONTEXT_MESSAGE_SCAN_LIMIT = 60
CONVERSATION_VIDEO_MODE_TO_PROVIDER_MODE = {
    "text_to_video": "text_to_video",
    "reference": "image_to_video",
    "first_last_frame": "first_last_frame",
}
COMFLY_VIDEO_VENDORS = {"comfly", "模型服务"}
ARK_REFERENCE_VIDEO_CONTENT_TYPES = {"video/mp4", "video/quicktime"}
ARK_REFERENCE_VIDEO_CODECS = {"h264", "hevc", "h265"}
ARK_REFERENCE_VIDEO_AUDIO_CODECS = {"aac", "mp3"}
ARK_REFERENCE_VIDEO_MIN_DURATION_SECONDS = 2
ARK_REFERENCE_VIDEO_MAX_DURATION_SECONDS = 15
ARK_REFERENCE_VIDEO_MAX_TOTAL_DURATION_SECONDS = 15
ARK_REFERENCE_VIDEO_MAX_ACCEPTED_DURATION_SECONDS = 15.2
ARK_REFERENCE_VIDEO_MAX_ACCEPTED_TOTAL_DURATION_SECONDS = 15.2
ARK_REFERENCE_VIDEO_DURATION_TOLERANCE_SECONDS = 0.2
ARK_REFERENCE_VIDEO_MAX_SIZE_BYTES = 200 * 1024 * 1024
ARK_REFERENCE_VIDEO_MIN_FPS = 24
ARK_REFERENCE_VIDEO_MAX_FPS = 60
ARK_REFERENCE_VIDEO_MIN_SIDE_PX = 300
ARK_REFERENCE_VIDEO_MAX_SIDE_PX = 6000
ARK_REFERENCE_VIDEO_MIN_PIXELS = 640 * 640
ARK_REFERENCE_VIDEO_MAX_PIXELS = 3326 * 2494
ARK_REFERENCE_VIDEO_MIN_RATIO = 0.4
ARK_REFERENCE_VIDEO_MAX_RATIO = 2.5
TEXT_MULTIMODAL_MEDIA_KEYS = (
    "images",
    "image",
    "image_url",
    "image_urls",
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
    "videos",
    "video",
    "video_url",
    "video_urls",
)
FIRST_FRAME_URL_KEYS = (
    "first_frame_url",
    "first_frame",
    "firstFrameUrl",
    "firstFrame",
    "first_image_url",
    "firstImageUrl",
    "start_frame_url",
    "start_frame",
    "startFrameUrl",
    "startFrame",
    "start_image_url",
    "startImageUrl",
    "reference_first_frame_url",
    "reference_start_frame_url",
)
LAST_FRAME_URL_KEYS = (
    "last_frame_url",
    "last_frame",
    "lastFrameUrl",
    "lastFrame",
    "last_image_url",
    "lastImageUrl",
    "end_frame_url",
    "end_frame",
    "endFrameUrl",
    "endFrame",
    "end_image_url",
    "endImageUrl",
    "ending_frame_url",
    "endingFrameUrl",
    "tail_frame_url",
    "tailFrameUrl",
    "reference_last_frame_url",
    "reference_end_frame_url",
)
FIRST_FRAME_ROLES = {
    "first_frame",
    "firstFrame",
    "start_frame",
    "startFrame",
    "reference_first_frame",
    "referenceFirstFrame",
    "reference_start_frame",
    "referenceStartFrame",
}
LAST_FRAME_ROLES = {
    "last_frame",
    "lastFrame",
    "end_frame",
    "endFrame",
    "ending_frame",
    "endingFrame",
    "tail_frame",
    "tailFrame",
    "reference_last_frame",
    "referenceLastFrame",
    "reference_end_frame",
    "referenceEndFrame",
}
REFERENCE_IMAGE_URL_KEYS = (
    "images",
    "image",
    "image_url",
    "image_urls",
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
REFERENCE_VIDEO_URL_KEYS = (
    "videos",
    "video",
    "video_url",
    "video_urls",
    "videoUrl",
    "videoUrls",
    "uploaded_videos",
    "uploadedVideos",
    "reference_video",
    "reference_video_url",
    "reference_videos",
    "reference_video_urls",
    "referenceVideo",
    "referenceVideoUrl",
    "referenceVideos",
    "referenceVideoUrls",
)
REFERENCE_AUDIO_URL_KEYS = (
    "audios",
    "audio",
    "audio_url",
    "audio_urls",
    "audioUrl",
    "audioUrls",
    "uploaded_audios",
    "uploadedAudios",
    "reference_audio",
    "reference_audio_url",
    "reference_audios",
    "reference_audio_urls",
    "referenceAudio",
    "referenceAudioUrl",
    "referenceAudios",
    "referenceAudioUrls",
)
GENERIC_UPLOAD_MEDIA_KEYS = (
    "file",
    "files",
    "file_list",
    "file_url",
    "file_urls",
    "fileList",
    "fileUrl",
    "fileUrls",
    "upload",
    "upload_file",
    "upload_files",
    "upload_list",
    "uploadFile",
    "uploadFiles",
    "uploadList",
    "uploads",
    "uploaded_file",
    "uploaded_files",
    "uploadedFile",
    "uploadedFiles",
    "attachment",
    "attachments",
    "attachment_url",
    "attachment_urls",
    "attachmentUrl",
    "attachmentUrls",
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
    conversation = await get_conversation_or_404(db, conversation_id, user_id)

    count_result = await db.execute(
        select(func.count())
        .select_from(ConversationMessage)
        .where(ConversationMessage.conversation_id == conversation_id)
    )
    total = count_result.scalar_one()

    if conversation.conversation_type == "text":
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
    else:
        order_by = (
            (ConversationMessage.created_at.desc(), ConversationMessage.id.desc())
            if order == "desc"
            else (ConversationMessage.created_at.asc(), ConversationMessage.id.asc())
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
    conversation = await get_conversation_or_404(db, conversation_id, user.id)
    if conversation.conversation_type == "text":
        await expire_stale_task_records(
            db,
            user_id=user.id,
            business_type="conversation",
            generation_type="text",
        )
        conversation = await _lock_conversation(db, conversation_id, user.id)
        existing_submission = await _find_idempotent_text_submission(
            db, conversation, payload
        )
        if existing_submission is not None:
            return existing_submission
        await _ensure_no_active_text_generation(db, conversation)

    else:
        conversation = await _lock_conversation(db, conversation_id, user.id)

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
    if conversation.ai_model_id != ai_model_db_id:
        conversation.ai_model_id = ai_model_db_id

    message_extra = await _build_message_extra_with_context(
        db,
        conversation_id=conversation_db_id,
        conversation_type=conversation_type,
        content=payload.content,
        extra=payload.extra or {},
        ai_model=ai_model,
    )
    _validate_conversation_request(
        ai_model, conversation_type, payload.content, message_extra
    )
    await ensure_model_minimum_balance(db, user.id, ai_model)
    ai_model_points_cost = calculate_submission_points_cost(
        ai_model, conversation_type, message_extra
    )

    points_transaction = None
    if ai_model_points_cost > 0:
        points_transaction = await consume_user_points(
            db,
            user_id=user.id,
            amount=ai_model_points_cost,
            remark=f"对话模型调用：{ai_model_nickname}",
            auto_commit=False,
        )

    turn_id = uuid4() if conversation_type == "text" else None
    first_sequence_no = (
        await _next_text_sequence_no(db, conversation_db_id)
        if conversation_type == "text"
        else None
    )
    user_message = ConversationMessage(
        conversation_id=conversation_db_id,
        user_id=user.id,
        role="user",
        content=payload.content,
        message_type=conversation_type,
        extra=payload.extra or {},
        ai_model_id=ai_model_db_id,
        turn_id=turn_id,
        sequence_no=first_sequence_no,
        status="success" if conversation_type == "text" else None,
        client_message_id=(payload.client_message_id if conversation_type == "text" else None),
    )
    assistant_message = ConversationMessage(
        conversation_id=conversation_db_id,
        user_id=user.id,
        role="assistant",
        content="任务已提交，正在生成中",
        message_type=conversation_type,
        extra={"task_status": "pending"},
        ai_model_id=ai_model_db_id,
        turn_id=turn_id,
        sequence_no=(first_sequence_no + 1 if first_sequence_no is not None else None),
        status="pending" if conversation_type == "text" else None,
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
            "turn_id": str(turn_id) if turn_id else None,
            "user_message_id": None,
            "assistant_message_id": None,
            "submission_points_cost": ai_model_points_cost,
            "user_message_extra": message_extra,
            "assistant_message_extra": {},
        },
        expire_stale=conversation_type != "text",
    )
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
    try:
        queue_name = _conversation_generation_queue(conversation_type)
        run_conversation_generation.apply_async(
            args=(str(task_record.id), str(assistant_message.id)),
            queue=queue_name,
            routing_key=queue_name,
        )
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


async def _next_text_sequence_no(db: AsyncSession, conversation_id: UUID) -> int:
    result = await db.execute(
        select(func.max(ConversationMessage.sequence_no)).where(
            ConversationMessage.conversation_id == conversation_id,
            ConversationMessage.message_type == "text",
        )
    )
    return int(result.scalar_one() or 0) + 1


async def retry_text_conversation_turn(
    db: AsyncSession,
    conversation_id: UUID,
    user_message_id: UUID,
    user: User,
) -> Tuple[ConversationMessage, ConversationMessage, int]:
    conversation = await get_conversation_or_404(db, conversation_id, user.id)
    if conversation.conversation_type != "text":
        raise AppException("只有文本会话支持失败重试", code=40014, status_code=400)

    await expire_stale_task_records(
        db,
        user_id=user.id,
        business_type="conversation",
        generation_type="text",
    )
    conversation = await _lock_conversation(db, conversation_id, user.id)
    await _ensure_no_active_text_generation(db, conversation)

    result = await db.execute(
        select(ConversationMessage).where(
            ConversationMessage.id == user_message_id,
            ConversationMessage.conversation_id == conversation.id,
            ConversationMessage.user_id == user.id,
            ConversationMessage.role == "user",
            ConversationMessage.message_type == "text",
        )
    )
    user_message = result.scalar_one_or_none()
    if user_message is None:
        raise AppException("文本用户消息不存在", code=40407, status_code=404)
    if user_message.turn_id is None:
        raise AppException("旧版文本消息不支持按轮次重试", code=40999, status_code=409)

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
    )
    _validate_conversation_request(ai_model, "text", user_message.content, message_extra)
    await ensure_model_minimum_balance(db, user.id, ai_model)
    points_cost = calculate_submission_points_cost(ai_model, "text", message_extra)
    points_transaction = None
    if points_cost > 0:
        points_transaction = await consume_user_points(
            db,
            user_id=user.id,
            amount=points_cost,
            remark=f"对话模型调用：{ai_model.nickname}",
            auto_commit=False,
        )

    assistant_message = ConversationMessage(
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
        sequence_no=await _next_text_sequence_no(db, conversation.id),
        status="pending",
    )
    db.add(assistant_message)
    task_record = await create_user_task_record(
        db,
        user_id=user.id,
        ai_model_id=ai_model.id,
        points_transaction_id=points_transaction.id if points_transaction else None,
        business_type="conversation",
        business_id=conversation.id,
        generation_type="text",
        status="pending",
        title=conversation.title,
        prompt=user_message.content,
        result=None,
        points_cost=points_cost,
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
        expire_stale=False,
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
    conversation.updated_at = beijing_datetime()
    await db.commit()
    await db.refresh(user_message)
    await db.refresh(assistant_message)

    try:
        run_conversation_generation.apply_async(
            args=(str(task_record.id), str(assistant_message.id)),
            queue="story_ai_text",
            routing_key="story_ai_text",
        )
    except Exception:
        await _mark_conversation_generation_enqueue_failed(
            db,
            task_record.id,
            assistant_message.id,
            user.id,
            points_cost,
            conversation.title,
        )
        await db.refresh(assistant_message)
    return user_message, assistant_message, points_cost


def _conversation_generation_queue(conversation_type: str) -> str:
    if conversation_type == "image":
        return "story_ai_image"
    if conversation_type == "video":
        return "story_ai_video"
    if conversation_type == "text":
        return "story_ai_text"
    return "story_ai_default"


def _validate_conversation_request(
    ai_model: AiModel,
    conversation_type: str,
    content: str,
    extra: Dict[str, Any],
) -> None:
    if ai_model.vendor == apimart.APIMART_VENDOR:
        if conversation_type == "text":
            apimart.validate_chat_completion_request(ai_model.model_id, content, extra)
            return
        if conversation_type == "image":
            apimart.validate_image_request(ai_model.model_id, content, extra)
            return
        if conversation_type == "video":
            apimart.validate_video_request(ai_model.model_id, content, extra)
            return
        return
    if not _is_comfly_model(ai_model):
        return
    if conversation_type == "text":
        comfly.validate_chat_completion_request(ai_model.model_id, content, extra)
        return
    if conversation_type == "image":
        comfly.validate_image_request(ai_model.model_id, content, extra)


def _is_comfly_model(ai_model: AiModel) -> bool:
    return ai_model.vendor in {"comfly", "模型服务"}


async def _build_message_extra_with_context(
    db: AsyncSession,
    conversation_id: UUID,
    conversation_type: str,
    content: str,
    extra: Dict[str, Any],
    ai_model: Optional[AiModel] = None,
) -> Dict[str, Any]:
    if conversation_type == "video":
        return await _build_video_message_extra(extra, ai_model)
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

    messages = await _build_text_context_messages(db, conversation_id, content, extra)
    return {**extra, "messages": messages}


async def _build_video_message_extra(
    extra: Dict[str, Any], ai_model: Optional[AiModel] = None
) -> Dict[str, Any]:
    generation_mode = _normalize_conversation_video_generation_mode(
        extra.get("generation_mode"), extra
    )
    payload = dict(extra)
    payload["generation_mode"] = generation_mode
    payload["resolution"] = _normalize_conversation_video_resolution(
        ai_model, payload.get("resolution")
    )
    provider_mode = CONVERSATION_VIDEO_MODE_TO_PROVIDER_MODE[generation_mode]
    payload["video_mode"] = provider_mode
    payload["capability"] = provider_mode

    if generation_mode == "text_to_video":
        if _has_video_media_input(payload):
            raise AppException(
                "文生视频不能传入参考图片、参考视频、参考音频或首尾帧图片",
                code=40012,
                status_code=400,
            )
        _drop_video_media_keys(payload)
        await _validate_conversation_video_model_capability(ai_model, payload, generation_mode)
        return payload

    if generation_mode == "reference":
        reference_images = _dedupe(
            [
                *_collect_reference_image_urls(payload),
                *_collect_media_image_urls(payload, {"reference_image"}, allow_roleless=True),
            ]
        )
        reference_videos = _dedupe(
            [
                *_collect_reference_video_urls(payload),
                *_collect_media_urls(payload, "video_url", {"reference_video"}),
            ]
        )
        reference_audios = _dedupe(
            [
                *_collect_reference_audio_urls(payload),
                *_collect_media_urls(payload, "audio_url", {"reference_audio"}),
            ]
        )
        if reference_images:
            payload["images"] = reference_images
            payload["image_urls"] = reference_images
        if reference_videos:
            payload["videos"] = reference_videos
            payload["video_urls"] = reference_videos
        if reference_audios:
            payload["audios"] = reference_audios
            payload["audio_urls"] = reference_audios
        allow_audio_only = _conversation_video_allows_audio_only(ai_model)
        if reference_audios and not (reference_images or reference_videos) and not allow_audio_only:
            raise AppException(
                "参考音频不能单独使用，需要同时传入参考图片或参考视频", code=40012, status_code=400
            )
        if not reference_images and not reference_videos and not (reference_audios and allow_audio_only):
            raise AppException(
                "参考生成需要至少传入参考图片或参考视频；上传后请把 /uploads/file 返回的 data.url 放入 extra.uploaded_images 或 extra.reference_video_url",
                code=40012,
                status_code=400,
            )
        provider_mode = _reference_video_provider_mode(
            reference_images, reference_videos, reference_audios
        )
        payload["video_mode"] = provider_mode
        payload["capability"] = provider_mode
        await _validate_conversation_video_model_capability(ai_model, payload, generation_mode)
        _drop_reference_media_source_keys(payload)
        _drop_frame_url_keys(payload)
        payload.pop("media", None)
        payload.pop("media_items", None)
        payload.pop("content", None)
        return payload

    first_frame_url = _extract_frame_url(payload, FIRST_FRAME_URL_KEYS, FIRST_FRAME_ROLES)
    last_frame_url = _extract_frame_url(payload, LAST_FRAME_URL_KEYS, LAST_FRAME_ROLES)
    if not first_frame_url:
        raise AppException("首尾帧生成需要传入 first_frame_url", code=40012, status_code=400)

    media_items = []
    payload["first_frame_url"] = first_frame_url
    media_items.append(
        {"type": "image_url", "image_url": {"url": first_frame_url}, "role": "first_frame"}
    )
    if last_frame_url:
        payload["last_frame_url"] = last_frame_url
        media_items.append(
            {"type": "image_url", "image_url": {"url": last_frame_url}, "role": "last_frame"}
        )
    payload["media_items"] = media_items
    payload.pop("images", None)
    payload.pop("image_urls", None)
    payload.pop("uploaded_images", None)
    payload.pop("reference_images", None)
    payload.pop("media", None)
    payload.pop("content", None)
    _drop_frame_url_keys(payload, keep={"first_frame_url", "last_frame_url"})
    await _validate_conversation_video_model_capability(ai_model, payload, generation_mode)
    return payload


def _reference_video_provider_mode(
    reference_images: List[str],
    reference_videos: List[str],
    reference_audios: List[str],
) -> str:
    if reference_videos:
        return "video_to_video"
    if reference_audios:
        return "audio_video"
    return "image_to_video"


async def _validate_conversation_video_model_capability(
    ai_model: Optional[AiModel],
    payload: Dict[str, Any],
    generation_mode: str,
) -> None:
    if ai_model is None:
        return

    capabilities = _conversation_video_capabilities(ai_model)
    if not capabilities:
        return

    modes = _capability_set(capabilities, "modes")
    allowed_keys = _capability_set(capabilities, "request_keys")
    has_input_constraints = bool(modes or allowed_keys or (capabilities.get("media_limits") or {}))
    has_images = _has_value(payload.get("images")) or _has_value(payload.get("image_urls"))
    has_videos = _has_value(payload.get("videos")) or _has_value(payload.get("video_urls"))
    has_audios = _has_value(payload.get("audios")) or _has_value(payload.get("audio_urls"))

    if generation_mode == "text_to_video":
        if modes and "text_to_video" not in modes:
            raise AppException(
                "当前模型不支持文生视频，请切换支持文生视频的模型", code=40012, status_code=400
            )
        return

    if generation_mode == "first_last_frame":
        if modes and "first_last_frame" not in modes:
            raise AppException(
                "当前模型不支持首尾帧生成，请切换支持首尾帧的模型", code=40012, status_code=400
            )
        if not _supports_image_reference(ai_model, capabilities, allowed_keys, modes):
            raise AppException(
                "当前模型不支持首尾帧图片输入，请切换支持图片输入的视频模型",
                code=40012,
                status_code=400,
            )
        return

    if generation_mode != "reference":
        return

    if has_videos and has_input_constraints:
        if not _supports_video_reference(ai_model, capabilities, allowed_keys, modes):
            raise AppException(
                "当前模型不支持参考视频生成，请切换支持参考视频的视频模型，或改用参考图/文生视频",
                code=40012,
                status_code=400,
            )
        if modes and not modes.intersection(
            {"reference", "multimodal_reference", "video_to_video"}
        ):
            raise AppException(
                "当前模型不支持参考视频生成，请切换支持参考视频的视频模型",
                code=40012,
                status_code=400,
            )

    if has_images and has_input_constraints:
        if not _supports_image_reference(ai_model, capabilities, allowed_keys, modes):
            raise AppException(
                "当前模型不支持参考图生成，请切换支持图片输入的视频模型，或改用文生视频",
                code=40012,
                status_code=400,
            )
        if modes and not modes.intersection(
            {"reference", "multimodal_reference", "image_to_video", "video_to_video", "audio_video"}
        ):
            raise AppException(
                "当前模型不支持参考图生成，请切换支持图片输入的视频模型",
                code=40012,
                status_code=400,
            )

    if has_audios and has_input_constraints:
        if not _supports_audio_reference(ai_model, capabilities, allowed_keys, modes):
            raise AppException(
                "当前模型不支持参考音频生成，请切换支持音频输入的视频模型",
                code=40012,
                status_code=400,
            )
        if modes and not modes.intersection({"reference", "multimodal_reference", "audio_video"}):
            raise AppException(
                "当前模型不支持参考音频生成，请切换支持音频输入的视频模型",
                code=40012,
                status_code=400,
            )

    _validate_conversation_video_media_limits(
        capabilities, has_images, has_videos, has_audios, payload
    )
    if has_videos and _is_ark_conversation_video_model(ai_model):
        await _validate_ark_reference_video_metadata(payload)


def _conversation_video_capabilities(ai_model: AiModel) -> Dict[str, Any]:
    saved = model_request_capabilities(ai_model)
    if ai_model.vendor == "volcengine_ark" or is_volcengine_ark_video_model(ai_model.model_id):
        return merge_ark_video_capabilities(ai_model.model_id, saved)
    if ai_model.vendor in COMFLY_VIDEO_VENDORS:
        return merge_comfly_video_capabilities(ai_model.model_id, saved)
    if ai_model.vendor == apimart.APIMART_VENDOR:
        return merge_apimart_video_capabilities(ai_model.model_id, saved)
    return saved


def _conversation_video_allows_audio_only(ai_model: Optional[AiModel]) -> bool:
    if ai_model is None:
        return False
    return bool(_conversation_video_capabilities(ai_model).get("allow_audio_only"))


def _capability_set(capabilities: Dict[str, Any], key: str) -> set[str]:
    return {str(item).strip() for item in (capabilities.get(key) or []) if str(item).strip()}


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(_has_value(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_has_value(item) for item in value)
    return True


def _supports_image_reference(
    ai_model: AiModel,
    capabilities: Dict[str, Any],
    allowed_keys: set[str],
    modes: set[str],
) -> bool:
    if _is_ark_conversation_video_model(ai_model):
        return _media_limit(capabilities, "images") != 0
    return (
        "images" in allowed_keys
        or _media_limit(capabilities, "images") > 0
        or bool(
            modes.intersection(
                {
                    "reference",
                    "multimodal_reference", "image_to_video", "video_to_video",
                    "audio_video",
                }
            )
        )
    )


def _supports_video_reference(
    ai_model: AiModel,
    capabilities: Dict[str, Any],
    allowed_keys: set[str],
    modes: set[str],
) -> bool:
    if _is_ark_conversation_video_model(ai_model):
        return _media_limit(capabilities, "videos") != 0
    return (
        "videos" in allowed_keys
        or _media_limit(capabilities, "videos") > 0
        or bool(modes.intersection({"reference", "multimodal_reference", "video_to_video"}))
    )


def _supports_audio_reference(
    ai_model: AiModel,
    capabilities: Dict[str, Any],
    allowed_keys: set[str],
    modes: set[str],
) -> bool:
    if _is_ark_conversation_video_model(ai_model):
        return _media_limit(capabilities, "audios", "audio") != 0
    return (
        "audio_url" in allowed_keys
        or _media_limit(capabilities, "audios", "audio") > 0
        or bool(modes.intersection({"reference", "multimodal_reference", "audio_video"}))
    )


def _is_ark_conversation_video_model(ai_model: AiModel) -> bool:
    return ai_model.vendor == "volcengine_ark" or is_volcengine_ark_video_model(ai_model.model_id)


def _media_limit(capabilities: Dict[str, Any], *keys: str) -> int:
    media_limits = capabilities.get("media_limits") or {}
    for key in keys:
        value = media_limits.get(key)
        if isinstance(value, int):
            return value
    return -1


def _validate_conversation_video_media_limits(
    capabilities: Dict[str, Any],
    has_images: bool,
    has_videos: bool,
    has_audios: bool,
    payload: Dict[str, Any],
) -> None:
    checks = (
        ("images", "参考图", has_images, payload.get("images") or payload.get("image_urls")),
        ("videos", "参考视频", has_videos, payload.get("videos") or payload.get("video_urls")),
        ("audios", "参考音频", has_audios, payload.get("audios") or payload.get("audio_urls")),
    )
    for key, label, enabled, values in checks:
        if not enabled:
            continue
        limit = _media_limit(capabilities, key, key.rstrip("s"))
        if limit > 0 and len(_as_list(values)) > limit:
            raise AppException(f"当前模型{label}最多支持 {limit} 个", code=40012, status_code=400)


async def _validate_ark_reference_video_metadata(payload: Dict[str, Any]) -> None:
    reference_urls = _reference_video_url_set(payload)
    uploaded_items = _collect_uploaded_media_items(payload, "video")
    existing_urls = {_extract_media_url(item) for item in uploaded_items}
    for url in reference_urls:
        if url and url not in existing_urls:
            uploaded_items.append({"url": url})

    if not uploaded_items:
        return

    total_duration = 0.0
    checked_urls: set[str] = set()
    for item in uploaded_items:
        url = _extract_media_url(item)
        if reference_urls and url not in reference_urls:
            continue
        if url in checked_urls:
            continue
        checked_urls.add(url)
        duration = await _validate_ark_reference_video_item(item)
        if duration is not None:
            total_duration += duration

    if total_duration > ARK_REFERENCE_VIDEO_MAX_ACCEPTED_TOTAL_DURATION_SECONDS:
        raise AppException(
            f"火山方舟参考视频总时长不能超过 15.2 秒，当前检测为 {_format_seconds(total_duration)} 秒",
            code=40012,
            status_code=400,
        )


async def _validate_ark_reference_video_item(item: Dict[str, Any]) -> Optional[float]:
    url = _extract_media_url(item)
    content_type = str(item.get("content_type") or item.get("mime_type") or "").strip().lower()
    if content_type and content_type not in ARK_REFERENCE_VIDEO_CONTENT_TYPES:
        raise AppException("火山方舟参考视频仅支持 mp4 或 mov 格式", code=40012, status_code=400)

    size = _optional_float(item.get("size"))
    if size is not None and size > ARK_REFERENCE_VIDEO_MAX_SIZE_BYTES:
        raise AppException("火山方舟参考视频单个文件不能超过 200MB", code=40012, status_code=400)

    media_info = _extract_media_info(item)
    if not media_info and url:
        media_info = await probe_media_url(url, "video") or {}
    if not media_info:
        return None

    duration = _optional_float(media_info.get("duration_seconds") or media_info.get("duration"))
    if duration is not None and not _is_ark_reference_video_duration_supported(duration):
        raise AppException(
            f"火山方舟参考视频单个时长必须在 2-15.2 秒之间，当前检测为 {_format_seconds(duration)} 秒",
            code=40012,
            status_code=400,
        )

    video_codec = (
        str(media_info.get("video_codec") or media_info.get("codec_name") or "").strip().lower()
    )
    if video_codec and video_codec not in ARK_REFERENCE_VIDEO_CODECS:
        raise AppException("火山方舟参考视频编码仅支持 H.264/H.265", code=40012, status_code=400)

    audio_codec = str(media_info.get("audio_codec") or "").strip().lower()
    if audio_codec and audio_codec not in ARK_REFERENCE_VIDEO_AUDIO_CODECS:
        raise AppException("火山方舟参考视频音频编码仅支持 AAC/MP3", code=40012, status_code=400)

    fps = _optional_float(media_info.get("fps") or media_info.get("frame_rate"))
    if fps is not None and not (ARK_REFERENCE_VIDEO_MIN_FPS <= fps <= ARK_REFERENCE_VIDEO_MAX_FPS):
        raise AppException("火山方舟参考视频帧率必须在 24-60 FPS 之间", code=40012, status_code=400)

    width = _optional_float(media_info.get("width"))
    height = _optional_float(media_info.get("height"))
    if width is not None and height is not None:
        _validate_ark_reference_video_dimensions(width, height)

    return duration


def _reference_video_url_set(payload: Dict[str, Any]) -> set[str]:
    urls: set[str] = set()
    for key in ("videos", "video_urls"):
        for value in _as_list(payload.get(key)):
            url = _extract_media_url(value)
            if url:
                urls.add(url)
    return urls


def _validate_ark_reference_video_dimensions(width: float, height: float) -> None:
    if width <= 0 or height <= 0:
        return
    if not (
        ARK_REFERENCE_VIDEO_MIN_SIDE_PX <= width <= ARK_REFERENCE_VIDEO_MAX_SIDE_PX
        and ARK_REFERENCE_VIDEO_MIN_SIDE_PX <= height <= ARK_REFERENCE_VIDEO_MAX_SIDE_PX
    ):
        raise AppException(
            "火山方舟参考视频宽高长度必须在 300-6000px 之间", code=40012, status_code=400
        )

    pixels = width * height
    if not (ARK_REFERENCE_VIDEO_MIN_PIXELS <= pixels <= ARK_REFERENCE_VIDEO_MAX_PIXELS):
        raise AppException("火山方舟参考视频总像素数不符合要求", code=40012, status_code=400)

    ratio = width / height
    if not (ARK_REFERENCE_VIDEO_MIN_RATIO <= ratio <= ARK_REFERENCE_VIDEO_MAX_RATIO):
        raise AppException("火山方舟参考视频宽高比必须在 0.4-2.5 之间", code=40012, status_code=400)


def _drop_reference_media_source_keys(payload: Dict[str, Any]) -> None:
    keep = {"images", "image_urls", "videos", "video_urls", "audios", "audio_urls"}
    for key in (
        *REFERENCE_IMAGE_URL_KEYS,
        *REFERENCE_VIDEO_URL_KEYS,
        *REFERENCE_AUDIO_URL_KEYS,
        *GENERIC_UPLOAD_MEDIA_KEYS,
    ):
        if key not in keep:
            payload.pop(key, None)


def _is_ark_reference_video_duration_supported(duration: float) -> bool:
    return (
        duration >= ARK_REFERENCE_VIDEO_MIN_DURATION_SECONDS - ARK_REFERENCE_VIDEO_DURATION_TOLERANCE_SECONDS
        and duration <= ARK_REFERENCE_VIDEO_MAX_ACCEPTED_DURATION_SECONDS
    )


def _format_seconds(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _collect_uploaded_media_items(extra: Dict[str, Any], media_type: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for key in GENERIC_UPLOAD_MEDIA_KEYS:
        for value in _as_list(extra.get(key)):
            if not isinstance(value, dict) or _uploaded_media_type(value) != media_type:
                continue
            items.append(value)
    return items


def _extract_media_info(item: Dict[str, Any]) -> Dict[str, Any]:
    for key in ("media_info", "metadata", "meta"):
        value = item.get(key)
        if isinstance(value, dict):
            return value
    for key in ("data", "response", "file", "upload"):
        value = item.get(key)
        if isinstance(value, dict):
            media_info = _extract_media_info(value)
            if media_info:
                return media_info
    return {}


def _optional_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_conversation_video_resolution(ai_model: Optional[AiModel], value: Any) -> str:
    if ai_model is not None and ai_model.vendor == apimart.APIMART_VENDOR:
        return normalize_apimart_video_resolution(ai_model.model_id, value)
    if ai_model is not None and (
        ai_model.vendor == "volcengine_ark" or is_volcengine_ark_video_model(ai_model.model_id)
    ):
        if value not in (None, "") and not is_known_video_resolution(value):
            raise AppException("火山方舟视频 resolution 参数不支持", code=40012, status_code=400)
        capabilities = merge_ark_video_capabilities(
            ai_model.model_id,
            model_request_capabilities(ai_model),
        )
        if value not in (None, "") and not is_video_resolution_supported(value, capabilities):
            raise AppException(
                "当前火山方舟视频模型不支持该 resolution 参数", code=40012, status_code=400
            )
        return normalize_video_resolution(value or "720p", capabilities)

    normalized = str(value or "720p").strip().lower()
    aliases = {
        "480": "480p",
        "720": "720p",
        "1080": "1080p",
        "hd": "720p",
        "fhd": "1080p",
    }
    normalized = aliases.get(normalized, normalized)
    return normalized if normalized in {"480p", "720p", "1080p"} else "720p"


def _normalize_conversation_video_generation_mode(
    value: Any, extra: Optional[Dict[str, Any]] = None
) -> str:
    if value in (None, ""):
        extra = extra or {}
        if _has_first_last_frame_input(extra):
            return "first_last_frame"
        if _has_reference_media_input(extra):
            return "reference"
        return "text_to_video"

    mode = str(value).strip()
    aliases = {
        "文生视频": "text_to_video",
        "文本生成视频": "text_to_video",
        "text": "text_to_video",
        "text2video": "text_to_video",
        "textToVideo": "text_to_video",
        "text_to_video": "text_to_video",
        "text-to-video": "text_to_video",
        "t2v": "text_to_video",
        "参考生成": "reference",
        "参考视频生成": "reference",
        "视频参考生成": "reference",
        "referenceGeneration": "reference",
        "reference_generation": "reference",
        "referenceVideo": "reference",
        "reference_video": "reference",
        "videoReference": "reference",
        "video_reference": "reference",
        "video-reference": "reference",
        "videoToVideo": "reference",
        "video_to_video": "reference",
        "video-to-video": "reference",
        "reference-video": "reference",
        "reference-generation": "reference",
        "参考音频生成": "reference",
        "音频参考生成": "reference",
        "referenceAudio": "reference",
        "reference_audio": "reference",
        "audioReference": "reference",
        "audio_reference": "reference",
        "audio-reference": "reference",
        "audioToVideo": "reference",
        "audio_to_video": "reference",
        "audio-to-video": "reference",
        "reference-audio": "reference",
        "imageToVideo": "reference",
        "image_to_video": "reference",
        "multimodalReference": "reference",
        "multimodal_reference": "reference",
        "多模态参考": "reference",
        "多模态参考生成": "reference",
        "参考图生成": "reference",
        "首帧生成": "first_last_frame",
        "首帧模式": "first_last_frame",
        "first_frame": "first_last_frame",
        "first-frame": "first_last_frame",
        "首尾帧生成": "first_last_frame",
        "首尾帧模式": "first_last_frame",
        "firstFrame": "first_last_frame",
        "first-last-frame": "first_last_frame",
        "firstLast": "first_last_frame",
        "firstLastFrame": "first_last_frame",
        "first_last": "first_last_frame",
    }
    mode = aliases.get(mode, mode)
    if mode not in CONVERSATION_VIDEO_MODE_TO_PROVIDER_MODE:
        raise AppException("不支持的视频生成方式", code=40012, status_code=400)
    return mode


def _has_first_last_frame_input(extra: Dict[str, Any]) -> bool:
    return bool(
        _extract_frame_url(extra, FIRST_FRAME_URL_KEYS, FIRST_FRAME_ROLES)
        or _extract_frame_url(extra, LAST_FRAME_URL_KEYS, LAST_FRAME_ROLES)
    )


def _has_reference_media_input(extra: Dict[str, Any]) -> bool:
    return bool(
        _collect_reference_image_urls(extra)
        or _collect_reference_video_urls(extra)
        or _collect_reference_audio_urls(extra)
        or _collect_media_image_urls(extra, {"reference_image"}, allow_roleless=True)
        or _collect_media_urls(extra, "video_url", {"reference_video"})
        or _collect_media_urls(extra, "audio_url", {"reference_audio"})
    )


def _has_video_media_input(extra: Dict[str, Any]) -> bool:
    return _has_first_last_frame_input(extra) or _has_reference_media_input(extra)


def _drop_video_media_keys(extra: Dict[str, Any]) -> None:
    for key in (
        "audio",
        "audio_url",
        "audio_urls",
        "audios",
        "content",
        "file",
        "file_url",
        "file_urls",
        "fileUrl",
        "fileUrls",
        "files",
        "image",
        "image_url",
        "image_urls",
        "imageUrl",
        "imageUrls",
        "images",
        "last_frame_url",
        "last_frame",
        "media",
        "media_items",
        "reference_audio",
        "reference_audio_url",
        "reference_audio_urls",
        "reference_audios",
        "reference_image",
        "reference_image_url",
        "reference_image_urls",
        "reference_images",
        "referenceImage",
        "referenceImageUrl",
        "referenceImageUrls",
        "referenceImages",
        "reference_video",
        "reference_video_url",
        "reference_video_urls",
        "reference_videos",
        "referenceVideo",
        "referenceVideoUrl",
        "referenceVideoUrls",
        "referenceVideos",
        "upload",
        "uploaded_file",
        "uploaded_files",
        "uploadedFile",
        "uploadedFiles",
        "uploaded_audios",
        "uploaded_images",
        "uploadedImages",
        "uploaded_videos",
        "uploadedVideos",
        "uploads",
        "attachment",
        "attachment_url",
        "attachment_urls",
        "attachmentUrl",
        "attachmentUrls",
        "attachments",
        "video",
        "video_url",
        "video_urls",
        "videoUrl",
        "videoUrls",
        "videos",
    ):
        extra.pop(key, None)
    _drop_frame_url_keys(extra)


def _collect_reference_image_urls(extra: Dict[str, Any]) -> List[str]:
    return [
        *_collect_extra_urls(extra, REFERENCE_IMAGE_URL_KEYS),
        *_collect_uploaded_media_urls(extra, "image"),
    ]


def _collect_reference_video_urls(extra: Dict[str, Any]) -> List[str]:
    return [
        *_collect_extra_urls(extra, REFERENCE_VIDEO_URL_KEYS),
        *_collect_uploaded_media_urls(extra, "video"),
    ]


def _collect_reference_audio_urls(extra: Dict[str, Any]) -> List[str]:
    return [
        *_collect_extra_urls(extra, REFERENCE_AUDIO_URL_KEYS),
        *_collect_uploaded_media_urls(extra, "audio"),
    ]


def _collect_uploaded_media_urls(extra: Dict[str, Any], media_type: str) -> List[str]:
    urls: List[str] = []
    for key in GENERIC_UPLOAD_MEDIA_KEYS:
        for value in _as_list(extra.get(key)):
            if _uploaded_media_type(value) != media_type:
                continue
            url = _extract_media_url(value)
            if url:
                urls.append(url)
    return urls


def _uploaded_media_type(value: Any) -> str:
    raw_type = ""
    if isinstance(value, dict):
        raw_type = _extract_upload_media_type(value)
    if raw_type.startswith("image/") or raw_type in {"image", "img", "image_url"}:
        return "image"
    if raw_type.startswith("video/") or raw_type in {"video", "video_url"}:
        return "video"
    if raw_type.startswith("audio/") or raw_type in {"audio", "audio_url"}:
        return "audio"

    url = _extract_media_url(value).lower().split("?", 1)[0]
    if url.endswith((".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".gif", ".heic", ".heif")):
        return "image"
    if url.endswith((".mp4", ".mov", ".webm", ".m4v", ".avi", ".mkv")):
        return "video"
    if url.endswith((".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg")):
        return "audio"
    return ""


def _extract_upload_media_type(value: Dict[str, Any]) -> str:
    for key in ("file_type", "media_type", "content_type", "mime_type", "type"):
        item = value.get(key)
        if item not in (None, ""):
            return str(item).strip().lower()
    for key in ("data", "response", "file", "upload"):
        nested = value.get(key)
        if isinstance(nested, dict):
            media_type = _extract_upload_media_type(nested)
            if media_type:
                return media_type
    return ""


def _collect_extra_urls(extra: Dict[str, Any], keys: Tuple[str, ...]) -> List[str]:
    urls: List[str] = []
    for key in keys:
        for value in _as_list(extra.get(key)):
            url = _extract_media_url(value)
            if url:
                urls.append(url)
    return urls


def _extract_frame_url(extra: Dict[str, Any], keys: Tuple[str, ...], roles: set[str]) -> str:
    for key in keys:
        url = _extract_media_url(extra.get(key))
        if url:
            return url
    for key in ("media_items", "media", "content"):
        for item in _as_list(extra.get(key)):
            if not isinstance(item, dict):
                continue
            role = _normalize_frame_role(item.get("role"))
            if role not in roles:
                continue
            url = _extract_media_url(item)
            if url:
                return url
    return ""


def _collect_media_image_urls(
    extra: Dict[str, Any], roles: set[str], *, allow_roleless: bool = False
) -> List[str]:
    urls: List[str] = []
    for key in ("media_items", "media", "content"):
        for item in _as_list(extra.get(key)):
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "image_url").strip()
            if item_type != "image_url":
                continue
            role = _normalize_frame_role(item.get("role"))
            if role not in roles and not (allow_roleless and not role):
                continue
            url = _extract_media_url(item)
            if url:
                urls.append(url)
    return urls


def _collect_media_urls(extra: Dict[str, Any], item_type: str, roles: set[str]) -> List[str]:
    urls: List[str] = []
    for key in ("media_items", "media", "content"):
        for item in _as_list(extra.get(key)):
            if not isinstance(item, dict):
                continue
            if str(item.get("type") or "").strip() != item_type:
                continue
            if _normalize_frame_role(item.get("role")) not in roles:
                continue
            url = _extract_media_url(item)
            if url:
                urls.append(url)
    return urls


def _normalize_frame_role(value: Any) -> str:
    role = str(value or "").strip().replace("-", "_")
    aliases = {
        "reference": "reference_image",
        "referenceImage": "reference_image",
        "ref_image": "reference_image",
        "refImage": "reference_image",
        "image": "reference_image",
        "video": "reference_video",
        "ref_video": "reference_video",
        "refVideo": "reference_video",
        "referenceVideo": "reference_video",
        "audio": "reference_audio",
        "ref_audio": "reference_audio",
        "refAudio": "reference_audio",
        "referenceAudio": "reference_audio",
        "firstFrame": "first_frame",
        "startFrame": "start_frame",
        "referenceFirstFrame": "reference_first_frame",
        "referenceStartFrame": "reference_start_frame",
        "lastFrame": "last_frame",
        "endFrame": "end_frame",
        "endingFrame": "ending_frame",
        "tailFrame": "tail_frame",
        "referenceLastFrame": "reference_last_frame",
        "referenceEndFrame": "reference_end_frame",
    }
    return aliases.get(role, role)


def _drop_frame_url_keys(extra: Dict[str, Any], keep: Optional[set[str]] = None) -> None:
    keep = keep or set()
    for key in (*FIRST_FRAME_URL_KEYS, *LAST_FRAME_URL_KEYS):
        if key not in keep:
            extra.pop(key, None)


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
        .order_by(
            ConversationMessage.sequence_no.desc().nullslast(),
            ConversationMessage.created_at.desc(),
            ConversationMessage.id.desc(),
        )
        .limit(DEFAULT_TEXT_CONTEXT_MESSAGE_SCAN_LIMIT)
    )
    history = list(reversed(result.scalars().all()))
    messages = _completed_text_context_messages(history)[-DEFAULT_TEXT_CONTEXT_MESSAGE_LIMIT:]
    messages.append(
        {
            "role": "user", "content": _build_text_multimodal_content(current_content, current_extra or {}),
        }
    )
    return messages


def _completed_text_context_messages(
    history: List[ConversationMessage],
) -> List[Dict[str, Any]]:
    turns: Dict[UUID, Dict[str, Any]] = {}
    ordered_entries: List[Tuple[str, Any]] = []

    for item in history:
        turn_id = getattr(item, "turn_id", None)
        if turn_id is None:
            if not _should_skip_text_context_message(item):
                ordered_entries.append(("legacy", item))
            continue

        if turn_id not in turns:
            turns[turn_id] = {"user": None, "assistant": None}
            ordered_entries.append(("turn", turn_id))

        status = getattr(item, "status", None)
        if item.role == "user" and status == "success" and item.content:
            turns[turn_id]["user"] = item
        elif item.role == "assistant" and status == "success" and item.content:
            turns[turn_id]["assistant"] = item

    messages: List[Dict[str, Any]] = []
    for entry_type, value in ordered_entries:
        if entry_type == "legacy":
            role = "assistant" if value.role == "assistant" else "user"
            content = (
                _build_text_multimodal_content(value.content, value.extra or {})
                if role == "user"
                else value.content
            )
            messages.append({"role": role, "content": content})
            continue

        turn = turns[value]
        if turn["user"] is None or turn["assistant"] is None:
            continue
        messages.extend(
            [
                {
                    "role": "user",
                    "content": _build_text_multimodal_content(
                        turn["user"].content,
                        turn["user"].extra or {},
                    ),
                },
                {"role": "assistant", "content": turn["assistant"].content},
            ]
        )
    return messages


def _should_skip_text_context_message(message: ConversationMessage) -> bool:
    if not message.content:
        return True
    if message.content.startswith("任务已提交") or message.content.startswith("任务执行失败"):
        return True
    if _is_placeholder_assistant_content(message.content):
        return True
    if message.role == "assistant" and (message.extra or {}).get("task_status") != "success":
        return True
    return False


def _is_placeholder_assistant_content(content: Any) -> bool:
    if not isinstance(content, str):
        return False
    return content.startswith("模型未返回有效内容")


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
        for key in ("url", "image_url", "video_url", "audio_url", "file_url", "oss_url"):
            nested = value.get(key)
            if isinstance(nested, str):
                return nested
            if isinstance(nested, dict) and isinstance(nested.get("url"), str):
                return nested["url"]
        for key in ("data", "response", "file", "upload"):
            nested = value.get(key)
            if isinstance(nested, dict):
                url = _extract_media_url(nested)
                if url:
                    return url
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
        if assistant_message.message_type == "text":
            assistant_message.status = "failed"
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

    result = await db.execute(
        select(UserTaskRecord)
        .where(
            UserTaskRecord.user_id == user.id,
            UserTaskRecord.business_type == "conversation",
            UserTaskRecord.business_id == conversation_id,
            UserTaskRecord.generation_type == conversation.conversation_type,
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
