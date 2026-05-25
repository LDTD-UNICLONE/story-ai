from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import AppException
from app.core.public_messages import sanitize_public_message
from app.core.timezone import beijing_datetime
from app.models.ai_model import AiModel
from app.models.conversation import ConversationMessage
from app.models.project_asset import ProjectCharacter, ProjectProp, ProjectScene
from app.models.project_chapter import ProjectChapter
from app.models.project_storyboard import ProjectStoryboard
from app.models.task_record import UserTaskRecord
from app.services.generated_media import persist_generated_media_to_oss
from app.services.model_runner import ModelRunResult, query_model_task
from app.services.points import change_user_points

TASK_RECORD_BUSINESS_TYPES = [
    {"label": "对话型", "value": "conversation"},
    {"label": "项目型", "value": "project"},
]

TASK_RECORD_GENERATION_TYPES = [
    {"label": "文本生成", "value": "text", "business_type": "conversation"},
    {"label": "图像生成", "value": "image", "business_type": "conversation"},
    {"label": "视频生成", "value": "video", "business_type": "conversation"},
    {"label": "章节文本处理", "value": "chapter_text_process", "business_type": "project"},
    {"label": "人物资产分析", "value": "character_analysis", "business_type": "project"},
    {"label": "场景资产分析", "value": "scene_analysis", "business_type": "project"},
    {"label": "道具资产分析", "value": "prop_analysis", "business_type": "project"},
    {"label": "分镜分析", "value": "storyboard_analysis", "business_type": "project"},
    {"label": "资产图像生成", "value": "asset_image_generate", "business_type": "project"},
    {"label": "分镜视频生成", "value": "storyboard_video", "business_type": "project"},
]

TASK_RECORD_STATUSES = [
    {"label": "待执行", "value": "pending"},
    {"label": "执行中", "value": "running"},
    {"label": "成功", "value": "success"},
    {"label": "失败", "value": "failed"},
]


def get_task_record_options() -> Dict[str, Any]:
    return {
        "business_types": TASK_RECORD_BUSINESS_TYPES,
        "generation_types": TASK_RECORD_GENERATION_TYPES,
        "statuses": TASK_RECORD_STATUSES,
    }


async def create_user_task_record(
    db: AsyncSession,
    user_id: UUID,
    business_type: str,
    generation_type: str,
    status: str,
    title: str,
    prompt: str,
    points_cost: int,
    ai_model_id: Optional[UUID] = None,
    business_id: Optional[UUID] = None,
    points_transaction_id: Optional[UUID] = None,
    result: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> UserTaskRecord:
    await expire_stale_task_records(db, user_id=user_id)
    await _ensure_user_task_capacity(db, user_id, generation_type)
    record = UserTaskRecord(
        user_id=user_id,
        ai_model_id=ai_model_id,
        points_transaction_id=points_transaction_id,
        business_type=business_type,
        business_id=business_id,
        generation_type=generation_type,
        status=status,
        title=title,
        prompt=prompt,
        result=result,
        points_cost=points_cost,
        extra=extra or {},
    )
    db.add(record)
    return record


async def _ensure_user_task_capacity(db: AsyncSession, user_id: UUID, generation_type: str) -> None:
    active_since = beijing_datetime() - timedelta(hours=max(1, settings.user_pending_task_window_hours))
    if settings.user_pending_task_limit > 0:
        total_result = await db.execute(
            select(func.count())
            .select_from(UserTaskRecord)
            .where(
                UserTaskRecord.user_id == user_id,
                UserTaskRecord.status.in_(("pending", "running")),
                UserTaskRecord.updated_at >= active_since,
            )
        )
        if total_result.scalar_one() >= settings.user_pending_task_limit:
            raise AppException("当前待处理任务较多，请等待部分任务完成后再提交", code=42901, status_code=429)

    if settings.user_pending_media_task_limit > 0 and generation_type in _media_generation_types():
        media_result = await db.execute(
            select(func.count())
            .select_from(UserTaskRecord)
            .where(
                UserTaskRecord.user_id == user_id,
                UserTaskRecord.status.in_(("pending", "running")),
                UserTaskRecord.generation_type.in_(_media_generation_types()),
                UserTaskRecord.updated_at >= active_since,
            )
        )
        if media_result.scalar_one() >= settings.user_pending_media_task_limit:
            raise AppException("当前图像或视频生成任务较多，请等待部分任务完成后再提交", code=42902, status_code=429)


def _media_generation_types() -> Tuple[str, ...]:
    return ("image", "video", "asset_image_generate", "storyboard_video")


async def list_task_records(
    db: AsyncSession,
    user_id: Optional[UUID],
    business_type: Optional[str],
    generation_type: Optional[str],
    status: Optional[str],
    page: int,
    page_size: int,
) -> Tuple[List[UserTaskRecord], int]:
    conditions = []
    if user_id:
        conditions.append(UserTaskRecord.user_id == user_id)
    if business_type:
        conditions.append(UserTaskRecord.business_type == business_type)
    if generation_type:
        conditions.append(UserTaskRecord.generation_type == generation_type)
    if status:
        conditions.append(UserTaskRecord.status == status)

    await expire_stale_task_records(
        db,
        user_id=user_id,
        business_type=business_type,
        generation_type=generation_type,
    )

    count_result = await db.execute(
        select(func.count()).select_from(UserTaskRecord).where(*conditions)
    )
    total = count_result.scalar_one()

    result = await db.execute(
        select(UserTaskRecord)
        .where(*conditions)
        .order_by(UserTaskRecord.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(result.scalars().all()), total


async def list_user_task_records(
    db: AsyncSession,
    user_id: UUID,
    business_type: Optional[str],
    generation_type: Optional[str],
    page: int,
    page_size: int,
) -> Tuple[List[UserTaskRecord], int]:
    return await list_task_records(
        db,
        user_id=user_id,
        business_type=business_type,
        generation_type=generation_type,
        status=None,
        page=page,
        page_size=page_size,
    )


async def get_task_record_or_404(
    db: AsyncSession,
    task_record_id: UUID,
    user_id: Optional[UUID] = None,
) -> UserTaskRecord:
    conditions = [UserTaskRecord.id == task_record_id]
    if user_id:
        conditions.append(UserTaskRecord.user_id == user_id)
    result = await db.execute(select(UserTaskRecord).where(*conditions))
    record = result.scalar_one_or_none()
    if record is None:
        raise AppException("任务记录不存在", code=40406, status_code=404)
    await expire_stale_task_record(db, record)
    await reconcile_provider_task_result(db, record)
    return record


async def reconcile_provider_task_result(db: AsyncSession, record: UserTaskRecord) -> None:
    if record.status not in {"pending", "running"}:
        return
    if await expire_stale_task_record(db, record):
        return

    provider_generation_type = _provider_generation_type(record)
    if provider_generation_type is None or record.ai_model_id is None:
        return

    task_id = _extract_provider_task_id(record.extra or {})
    if not task_id:
        return
    if _should_skip_provider_reconcile(record.extra or {}):
        return

    ai_model = await db.get(AiModel, record.ai_model_id)
    if ai_model is None:
        return

    try:
        model_result = await query_model_task(ai_model, provider_generation_type, task_id)
    except Exception:
        await _mark_next_reconcile(db, record, None)
        return

    status = str(model_result.extra.get("task_status") or "").lower()
    if _is_provider_failed_status(status):
        await _mark_reconciled_failed(db, record, f"模型任务执行失败：{status or 'failed'}", model_result.extra)
        return

    if not _is_provider_success_result(model_result, status):
        record.extra = {
            **(record.extra or {}),
            "last_provider_task_status": model_result.extra,
            "provider_reconciled_at": beijing_datetime().isoformat(),
            "next_poll_seconds": _provider_reconcile_interval(),
        }
        await db.commit()
        return

    model_result.extra = {**model_result.extra, "platform_task_status": "success"}
    model_result = await persist_generated_media_to_oss(provider_generation_type, model_result)
    await _mark_reconciled_success(db, record, provider_generation_type, model_result)


def _provider_generation_type(record: UserTaskRecord) -> Optional[str]:
    if record.business_type == "conversation" and record.generation_type in {"image", "video"}:
        return record.generation_type
    if record.business_type == "project" and record.generation_type == "asset_image_generate":
        return "image"
    if record.business_type == "project" and record.generation_type == "storyboard_video":
        return "video"
    return None


def _extract_provider_task_id(extra: Dict[str, Any]) -> Optional[str]:
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


async def _mark_next_reconcile(
    db: AsyncSession,
    record: UserTaskRecord,
    provider_extra: Optional[Dict[str, Any]],
) -> None:
    extra = {
        **(record.extra or {}),
        "provider_reconciled_at": beijing_datetime().isoformat(),
        "next_poll_seconds": _provider_reconcile_interval(),
    }
    if provider_extra is not None:
        extra["last_provider_task_status"] = provider_extra
    record.extra = extra
    await db.commit()


def _should_skip_provider_reconcile(extra: Dict[str, Any]) -> bool:
    reconciled_at = extra.get("provider_reconciled_at")
    if not reconciled_at:
        return False
    try:
        last_time = datetime.fromisoformat(str(reconciled_at))
    except ValueError:
        return False
    elapsed = (beijing_datetime() - last_time).total_seconds()
    return elapsed < _provider_reconcile_interval()


def _provider_reconcile_interval() -> int:
    return max(5, settings.provider_task_poll_interval_seconds)


async def expire_stale_task_records(
    db: AsyncSession,
    user_id: Optional[UUID] = None,
    business_type: Optional[str] = None,
    generation_type: Optional[str] = None,
    limit: int = 100,
) -> int:
    conditions = [
        UserTaskRecord.status.in_(("pending", "running")),
        UserTaskRecord.created_at <= _stale_task_cutoff(),
    ]
    if user_id:
        conditions.append(UserTaskRecord.user_id == user_id)
    if business_type:
        conditions.append(UserTaskRecord.business_type == business_type)
    if generation_type:
        conditions.append(UserTaskRecord.generation_type == generation_type)

    result = await db.execute(
        select(UserTaskRecord)
        .where(*conditions)
        .order_by(UserTaskRecord.created_at.asc())
        .limit(limit)
    )
    records = list(result.scalars().all())
    for record in records:
        await _mark_stale_failed(db, record)
    return len(records)


async def expire_stale_task_record(db: AsyncSession, record: UserTaskRecord) -> bool:
    if record.status not in {"pending", "running"}:
        return False
    if record.created_at > _stale_task_cutoff():
        return False
    await _mark_stale_failed(db, record)
    await db.refresh(record)
    return True


def _stale_task_cutoff() -> datetime:
    timeout_minutes = max(1, settings.task_stale_timeout_minutes)
    return beijing_datetime() - timedelta(minutes=timeout_minutes)


async def _mark_stale_failed(db: AsyncSession, record: UserTaskRecord) -> None:
    reason = f"任务超过 {max(1, settings.task_stale_timeout_minutes)} 分钟未完成，已自动判定失败"
    record.status = "failed"
    record.result = reason
    record.extra = {
        **(record.extra or {}),
        "failed_reason": reason,
        "stale_failed": True,
        "stale_failed_at": beijing_datetime().isoformat(),
    }
    await _refund_stale_task_points(db, record)
    await _sync_stale_failed_business_state(db, record, reason)
    await db.commit()


async def _refund_stale_task_points(db: AsyncSession, record: UserTaskRecord) -> None:
    if record.points_cost <= 0 or (record.extra or {}).get("refund_transaction_id"):
        return
    refund_transaction = await change_user_points(
        db,
        user_id=record.user_id,
        amount=record.points_cost,
        transaction_type="refund",
        remark=f"任务超时失败退回积分：{record.title}",
        auto_commit=False,
    )
    record.extra = {
        **(record.extra or {}),
        "refund_transaction_id": str(refund_transaction.id),
    }


async def _sync_stale_failed_business_state(
    db: AsyncSession,
    record: UserTaskRecord,
    reason: str,
) -> None:
    if record.business_type == "conversation":
        await _sync_conversation_failed(db, record, reason)
        return
    if record.generation_type == "chapter_text_process":
        await _sync_chapter_text_failed(db, record, reason)
        return
    if record.generation_type in {"character_analysis", "scene_analysis", "prop_analysis", "storyboard_analysis"}:
        await _sync_chapter_analysis_failed(db, record, reason)
        return
    if record.generation_type == "asset_image_generate":
        await _sync_asset_image_failed(db, record, reason)
        return
    if record.generation_type == "storyboard_video":
        await _sync_storyboard_video_failed(db, record, reason)


async def _sync_conversation_failed(db: AsyncSession, record: UserTaskRecord, reason: str) -> None:
    assistant_message_id = (record.extra or {}).get("assistant_message_id")
    parsed_assistant_message_id = _parse_uuid(assistant_message_id)
    if parsed_assistant_message_id is None:
        return
    assistant_message = await db.get(ConversationMessage, parsed_assistant_message_id)
    if assistant_message is None:
        return
    assistant_message.content = f"任务执行失败：{reason}"
    assistant_message.extra = {
        **(assistant_message.extra or {}),
        "task_status": "failed",
        "failed_reason": reason,
        "task_record_id": str(record.id),
    }


async def _sync_chapter_text_failed(db: AsyncSession, record: UserTaskRecord, reason: str) -> None:
    chapter_id = (record.extra or {}).get("chapter_id")
    parsed_chapter_id = _parse_uuid(chapter_id)
    if parsed_chapter_id is None:
        return
    chapter = await db.get(ProjectChapter, parsed_chapter_id)
    if chapter is None:
        return
    chapter.process_status = "failed"
    chapter.extra = {
        **(chapter.extra or {}),
        "failed_reason": reason,
        "task_record_id": str(record.id),
    }


async def _sync_chapter_analysis_failed(db: AsyncSession, record: UserTaskRecord, reason: str) -> None:
    chapter_id = (record.extra or {}).get("chapter_id")
    parsed_chapter_id = _parse_uuid(chapter_id)
    if parsed_chapter_id is None:
        return
    chapter = await db.get(ProjectChapter, parsed_chapter_id)
    if chapter is None:
        return
    status_key = {
        "character_analysis": "character_analysis_status",
        "scene_analysis": "scene_analysis_status",
        "prop_analysis": "prop_analysis_status",
        "storyboard_analysis": "storyboard_analysis_status",
    }.get(record.generation_type)
    task_key = {
        "character_analysis": "character_analysis_task_record_id",
        "scene_analysis": "scene_analysis_task_record_id",
        "prop_analysis": "prop_analysis_task_record_id",
        "storyboard_analysis": "storyboard_analysis_task_record_id",
    }.get(record.generation_type)
    if not status_key:
        return
    chapter.extra = {
        **(chapter.extra or {}),
        status_key: "failed",
        f"{status_key}_failed_reason": reason,
        **({task_key: str(record.id)} if task_key else {}),
    }


async def _sync_asset_image_failed(db: AsyncSession, record: UserTaskRecord, reason: str) -> None:
    asset_type = (record.extra or {}).get("asset_type")
    asset_id = (record.extra or {}).get("asset_id")
    model = {
        "character": ProjectCharacter,
        "scene": ProjectScene,
        "prop": ProjectProp,
    }.get(str(asset_type))
    parsed_asset_id = _parse_uuid(asset_id)
    if model is None or parsed_asset_id is None:
        return
    asset = await db.get(model, parsed_asset_id)
    if asset is None:
        return
    asset.extra = {
        **(asset.extra or {}),
        "image_generation_status": "failed",
        "image_generation_failed_reason": reason,
        "image_generation_task_record_id": str(record.id),
    }


async def _sync_storyboard_video_failed(db: AsyncSession, record: UserTaskRecord, reason: str) -> None:
    storyboard_id = (record.extra or {}).get("storyboard_id")
    parsed_storyboard_id = _parse_uuid(storyboard_id)
    if parsed_storyboard_id is None:
        return
    storyboard = await db.get(ProjectStoryboard, parsed_storyboard_id)
    if storyboard is None:
        return
    storyboard.extra = {
        **(storyboard.extra or {}),
        "video_generation_status": "failed",
        "video_generation_failed_reason": reason,
        "video_generation_task_record_id": str(record.id),
    }


async def _mark_reconciled_success(
    db: AsyncSession,
    record: UserTaskRecord,
    provider_generation_type: str,
    model_result: ModelRunResult,
) -> None:
    record.status = "success"
    record.result = model_result.content
    record.extra = {
        **(record.extra or {}),
        "model_result_extra": model_result.extra,
        "provider_reconciled_at": beijing_datetime().isoformat(),
    }

    if record.business_type == "conversation":
        await _sync_conversation_message_success(db, record, model_result)
    elif record.generation_type == "asset_image_generate":
        await _sync_asset_image_success(db, record, model_result)
    elif record.generation_type == "storyboard_video":
        await _sync_storyboard_video_success(db, record, model_result)

    await db.commit()
    await db.refresh(record)


async def _mark_reconciled_failed(
    db: AsyncSession,
    record: UserTaskRecord,
    reason: str,
    provider_extra: Dict[str, Any],
) -> None:
    reason = sanitize_public_message(reason)
    record.status = "failed"
    record.result = reason
    record.extra = {
        **(record.extra or {}),
        "failed_reason": reason,
        "model_result_extra": provider_extra,
        "provider_reconciled_at": beijing_datetime().isoformat(),
    }
    if record.business_type == "conversation":
        assistant_message_id = (record.extra or {}).get("assistant_message_id")
        parsed_assistant_message_id = _parse_uuid(assistant_message_id)
        if parsed_assistant_message_id:
            assistant_message = await db.get(ConversationMessage, parsed_assistant_message_id)
            if assistant_message:
                assistant_message.content = f"任务执行失败：{reason}"
                assistant_message.extra = {
                    **(assistant_message.extra or {}),
                    "task_status": "failed",
                    "failed_reason": reason,
                    "task_record_id": str(record.id),
                }
    elif record.generation_type == "storyboard_video":
        storyboard_id = (record.extra or {}).get("storyboard_id")
        parsed_storyboard_id = _parse_uuid(storyboard_id)
        if parsed_storyboard_id:
            storyboard = await db.get(ProjectStoryboard, parsed_storyboard_id)
            if storyboard:
                storyboard.extra = {
                    **(storyboard.extra or {}),
                    "video_generation_status": "failed",
                    "video_generation_failed_reason": reason,
                    "video_generation_task_record_id": str(record.id),
                }
    await db.commit()
    await db.refresh(record)


async def _sync_conversation_message_success(
    db: AsyncSession,
    record: UserTaskRecord,
    model_result: ModelRunResult,
) -> None:
    assistant_message_id = (record.extra or {}).get("assistant_message_id")
    parsed_assistant_message_id = _parse_uuid(assistant_message_id)
    if parsed_assistant_message_id is None:
        return
    assistant_message = await db.get(ConversationMessage, parsed_assistant_message_id)
    if assistant_message is None:
        return
    assistant_message.content = model_result.content
    assistant_message.extra = {
        **(assistant_message.extra or {}),
        **model_result.extra,
        "task_status": "success",
        "task_record_id": str(record.id),
    }
    record.extra = {
        **(record.extra or {}),
        "assistant_message_extra": model_result.extra,
        "assistant_message_id": str(assistant_message.id),
    }


async def _sync_asset_image_success(
    db: AsyncSession,
    record: UserTaskRecord,
    model_result: ModelRunResult,
) -> None:
    asset_type = (record.extra or {}).get("asset_type")
    asset_id = (record.extra or {}).get("asset_id")
    model = {
        "character": ProjectCharacter,
        "scene": ProjectScene,
        "prop": ProjectProp,
    }.get(str(asset_type))
    if model is None or not asset_id:
        return
    parsed_asset_id = _parse_uuid(asset_id)
    if parsed_asset_id is None:
        return
    asset = await db.get(model, parsed_asset_id)
    if asset is None:
        return
    image_url = _first_result_url(model_result.content)
    if not image_url:
        return
    asset.reference_image = image_url
    asset.updated_at = beijing_datetime()
    asset.extra = {
        **(asset.extra or {}),
        "image_generation_status": "success",
        "image_generation_task_record_id": str(record.id),
        "image_generation_extra": model_result.extra,
    }
    record.result = image_url
    record.extra = {
        **(record.extra or {}),
        "oss_image_url": image_url,
    }


async def _sync_storyboard_video_success(
    db: AsyncSession,
    record: UserTaskRecord,
    model_result: ModelRunResult,
) -> None:
    storyboard_id = (record.extra or {}).get("storyboard_id")
    parsed_storyboard_id = _parse_uuid(storyboard_id)
    if parsed_storyboard_id is None:
        return
    storyboard = await db.get(ProjectStoryboard, parsed_storyboard_id)
    if storyboard is None:
        return
    storyboard.extra = {
        **(storyboard.extra or {}),
        "video_generation_status": "success",
        "video_generation_task_record_id": str(record.id),
        "video_generation_result": model_result.content,
        "video_generation_extra": model_result.extra,
    }
    storyboard.updated_at = beijing_datetime()
    record.extra = {
        **(record.extra or {}),
        "storyboard_video_result": model_result.content,
    }


def _first_result_url(content: str) -> str:
    for value in (content or "").split(","):
        url = value.strip()
        if url.startswith(("http://", "https://")):
            return url
    return ""


def _parse_uuid(value: Any) -> Optional[UUID]:
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _is_provider_failed_status(status: str) -> bool:
    return status in {"failed", "failure", "error", "cancelled", "canceled"}


def _is_provider_success_result(model_result: ModelRunResult, status: str) -> bool:
    if status in {"success", "succeeded", "completed", "complete", "finished", "done"}:
        return True
    content = (model_result.content or "").strip()
    return bool(content and content != "生成任务处理中" and not content.startswith("模型任务仍在生成中"))
