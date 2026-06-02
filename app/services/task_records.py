from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import AppException
from app.core.public_messages import sanitize_public_message
from app.core.timezone import beijing_datetime, to_beijing_datetime
from app.models.ai_model import AiModel
from app.models.conversation import ConversationMessage
from app.models.project_asset import ProjectCharacter, ProjectProp, ProjectScene
from app.models.project_chapter import ProjectChapter
from app.models.project_storyboard import ProjectStoryboard
from app.models.task_record import UserTaskRecord
from app.services.generated_media import persist_generated_media_to_oss
from app.services.model_points import settle_video_task_points
from app.services.model_runner import ModelRunResult, query_model_task
from app.services.points import change_user_points
from app.services.project_generated_assets import create_project_generated_asset_history, extract_result_urls
from app.services.provider_polling import provider_poll_interval_seconds

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
    {"label": "分镜制作", "value": "storyboard_analysis", "business_type": "project"},
    {"label": "分镜细化字段生成", "value": "storyboard_refinement", "business_type": "project"},
    {"label": "故事板提示词生成", "value": "storyboard_image_prompt", "business_type": "project"},
    {"label": "资产图像生成", "value": "asset_image_generate", "business_type": "project"},
    {"label": "分镜故事板图像生成", "value": "storyboard_image", "business_type": "project"},
    {"label": "分镜视频生成", "value": "storyboard_video", "business_type": "project"},
]

TASK_RECORD_STATUSES = [
    {"label": "待执行", "value": "pending"},
    {"label": "执行中", "value": "running"},
    {"label": "成功", "value": "success"},
    {"label": "失败", "value": "failed"},
]


@dataclass
class ProviderReconcileClaim:
    task_record_id: UUID
    claim_id: str
    provider_generation_type: str
    provider_task_id: str
    ai_model: AiModel


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
    queue_snapshot = await _build_user_task_queue_snapshot(db, user_id, generation_type)
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
        extra={
            **(extra or {}),
            "queue_snapshot": queue_snapshot,
        },
    )
    db.add(record)
    return record


async def _build_user_task_queue_snapshot(
    db: AsyncSession,
    user_id: UUID,
    generation_type: str,
) -> Dict[str, Any]:
    active_since = beijing_datetime() - timedelta(hours=max(1, settings.user_pending_task_window_hours))
    snapshot: Dict[str, Any] = {
        "queued_at": beijing_datetime().isoformat(),
        "generation_type": generation_type,
        "window_hours": max(1, settings.user_pending_task_window_hours),
        "active_tasks_before": 0,
        "same_type_active_tasks_before": 0,
        "active_media_tasks_before": 0,
        "task_queue_position": 1,
        "same_type_queue_position": 1,
        "media_queue_position": 1,
        "configured_task_limit": max(0, settings.user_pending_task_limit),
        "configured_media_task_limit": max(0, settings.user_pending_media_task_limit),
        "exceeds_configured_task_limit": False,
        "exceeds_configured_media_task_limit": False,
        "limits_block_submission": False,
    }

    total_result = await db.execute(
        select(func.count())
        .select_from(UserTaskRecord)
        .where(
            UserTaskRecord.user_id == user_id,
            UserTaskRecord.status.in_(("pending", "running")),
            UserTaskRecord.updated_at >= active_since,
        )
    )
    active_tasks_before = int(total_result.scalar_one() or 0)
    snapshot["active_tasks_before"] = active_tasks_before
    snapshot["task_queue_position"] = active_tasks_before + 1

    same_type_result = await db.execute(
        select(func.count())
        .select_from(UserTaskRecord)
        .where(
            UserTaskRecord.user_id == user_id,
            UserTaskRecord.status.in_(("pending", "running")),
            UserTaskRecord.generation_type == generation_type,
            UserTaskRecord.updated_at >= active_since,
        )
    )
    same_type_tasks_before = int(same_type_result.scalar_one() or 0)
    snapshot["same_type_active_tasks_before"] = same_type_tasks_before
    snapshot["same_type_queue_position"] = same_type_tasks_before + 1

    if settings.user_pending_task_limit > 0:
        snapshot["exceeds_configured_task_limit"] = active_tasks_before >= settings.user_pending_task_limit

    if generation_type in _media_generation_types():
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
        active_media_tasks_before = int(media_result.scalar_one() or 0)
        snapshot["active_media_tasks_before"] = active_media_tasks_before
        snapshot["media_queue_position"] = active_media_tasks_before + 1
        if settings.user_pending_media_task_limit > 0:
            snapshot["exceeds_configured_media_task_limit"] = (
                active_media_tasks_before >= settings.user_pending_media_task_limit
            )
    return snapshot


def _media_generation_types() -> Tuple[str, ...]:
    return ("image", "video", "asset_image_generate", "storyboard_image", "storyboard_video")


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
        .order_by(UserTaskRecord.created_at.desc(), UserTaskRecord.id.desc())
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


async def list_user_task_records_by_ids(
    db: AsyncSession,
    user_id: UUID,
    task_record_ids: List[UUID],
) -> List[UserTaskRecord]:
    unique_ids = list(dict.fromkeys(task_record_ids))
    if not unique_ids:
        return []

    result = await db.execute(
        select(UserTaskRecord).where(
            UserTaskRecord.id.in_(unique_ids),
            UserTaskRecord.user_id == user_id,
        )
    )
    records_by_id = {record.id: record for record in result.scalars().all()}
    records = [records_by_id[task_record_id] for task_record_id in unique_ids if task_record_id in records_by_id]
    for record in records:
        await expire_stale_task_record(db, record)
    return records


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
    return record


def is_task_record_interrupted(record: UserTaskRecord) -> bool:
    return bool((record.extra or {}).get("interrupted"))


async def refresh_task_record_interrupted(db: AsyncSession, record: UserTaskRecord) -> bool:
    await db.refresh(record)
    return is_task_record_interrupted(record)


async def interrupt_task_record(
    db: AsyncSession,
    task_record_id: UUID,
    admin_user_id: UUID,
    reason: Optional[str] = None,
) -> UserTaskRecord:
    result = await db.execute(
        select(UserTaskRecord)
        .where(UserTaskRecord.id == task_record_id)
        .with_for_update()
    )
    record = result.scalar_one_or_none()
    if record is None:
        raise AppException("任务记录不存在", code=40406, status_code=404)
    if record.status in {"success", "failed"}:
        raise AppException("任务已结束，不能中断", code=40035, status_code=400)

    public_reason = sanitize_public_message(reason or "任务已被管理员中断，生成失败")
    record.status = "failed"
    record.result = public_reason
    record.extra = {
        **(record.extra or {}),
        "failed_reason": public_reason,
        "interrupted": True,
        "interrupted_by": str(admin_user_id),
        "interrupted_at": beijing_datetime().isoformat(),
    }
    await _refund_interrupted_task_points(db, record)
    await _sync_stale_failed_business_state(db, record, public_reason)
    await db.commit()
    await db.refresh(record)
    return record


async def reconcile_provider_task_record(db: AsyncSession, task_record_id: UUID) -> Optional[UserTaskRecord]:
    claim = await _claim_provider_reconcile(db, task_record_id)
    if claim is None:
        return None

    try:
        model_result = await query_model_task(
            claim.ai_model,
            claim.provider_generation_type,
            claim.provider_task_id,
        )
    except Exception:
        return await _finish_provider_reconcile_claim(
            db,
            claim,
            model_result=None,
            query_failed=True,
        )

    status = str(model_result.extra.get("task_status") or "").lower()
    if _is_provider_success_result(model_result, status):
        model_result.extra = {**model_result.extra, "platform_task_status": "success"}
        try:
            model_result = await persist_generated_media_to_oss(claim.provider_generation_type, model_result)
        except Exception:
            return await _finish_provider_reconcile_claim(
                db,
                claim,
                model_result=None,
                query_failed=True,
            )

    return await _finish_provider_reconcile_claim(
        db,
        claim,
        model_result=model_result,
        query_failed=False,
    )


async def reconcile_provider_task_result(db: AsyncSession, record: UserTaskRecord) -> None:
    await reconcile_provider_task_record(db, record.id)


async def _claim_provider_reconcile(db: AsyncSession, task_record_id: UUID) -> Optional[ProviderReconcileClaim]:
    result = await db.execute(
        select(UserTaskRecord)
        .where(UserTaskRecord.id == task_record_id)
        .with_for_update(skip_locked=True)
    )
    record = result.scalar_one_or_none()
    if record is None:
        await db.rollback()
        return None
    if record.status not in {"pending", "running"}:
        await db.rollback()
        return None
    if await expire_stale_task_record(db, record):
        return None

    provider_generation_type = _provider_generation_type(record)
    if provider_generation_type is None or record.ai_model_id is None:
        await db.rollback()
        return None

    task_id = _extract_provider_task_id(record.extra or {})
    if not task_id or _should_skip_provider_reconcile(record):
        await db.rollback()
        return None
    if _has_active_provider_reconcile_claim(record.extra or {}):
        await db.rollback()
        return None

    ai_model = await db.get(AiModel, record.ai_model_id)
    if ai_model is None:
        await db.rollback()
        return None

    claim_id = str(uuid4())
    now = beijing_datetime()
    record.extra = {
        **(record.extra or {}),
        "provider_reconcile_claim_id": claim_id,
        "provider_reconcile_claimed_at": now.isoformat(),
        "provider_reconcile_claim_until": (now + timedelta(seconds=_provider_reconcile_lease_seconds())).isoformat(),
    }
    await db.commit()
    return ProviderReconcileClaim(
        task_record_id=record.id,
        claim_id=claim_id,
        provider_generation_type=provider_generation_type,
        provider_task_id=task_id,
        ai_model=ai_model,
    )


async def _finish_provider_reconcile_claim(
    db: AsyncSession,
    claim: ProviderReconcileClaim,
    *,
    model_result: Optional[ModelRunResult],
    query_failed: bool,
) -> Optional[UserTaskRecord]:
    result = await db.execute(
        select(UserTaskRecord)
        .where(UserTaskRecord.id == claim.task_record_id)
        .with_for_update(skip_locked=True)
    )
    record = result.scalar_one_or_none()
    if record is None:
        await db.rollback()
        return None
    if record.status not in {"pending", "running"}:
        await db.rollback()
        return record
    if (record.extra or {}).get("provider_reconcile_claim_id") != claim.claim_id:
        await db.rollback()
        return None

    if query_failed or model_result is None:
        await _mark_next_reconcile(db, record, None)
        await db.refresh(record)
        return record

    status = str(model_result.extra.get("task_status") or "").lower()
    if _is_provider_failed_status(status):
        await _mark_reconciled_failed(db, record, f"模型任务执行失败：{status or 'failed'}", model_result.extra)
    elif not _is_provider_success_result(model_result, status):
        record.extra = _clear_provider_reconcile_claim(
            {
                **(record.extra or {}),
                "last_provider_task_status": model_result.extra,
                "provider_reconciled_at": beijing_datetime().isoformat(),
                "next_poll_seconds": _provider_reconcile_interval(record),
            }
        )
        await db.commit()
        await db.refresh(record)
    else:
        await _mark_reconciled_success(db, record, claim.provider_generation_type, model_result)
    return record


def should_reconcile_provider_task(record: UserTaskRecord) -> bool:
    return (
        record.status in {"pending", "running"}
        and _provider_generation_type(record) is not None
        and bool(_extract_provider_task_id(record.extra or {}))
        and not _has_active_provider_reconcile_claim(record.extra or {})
    )


def provider_reconcile_delay_seconds(record: Optional[UserTaskRecord] = None) -> int:
    if record is not None:
        next_poll_seconds = (record.extra or {}).get("next_poll_seconds")
        if isinstance(next_poll_seconds, int) and next_poll_seconds > 0:
            return next_poll_seconds
        return _provider_reconcile_interval(record)
    return _provider_reconcile_interval(None)


async def list_provider_reconcile_candidates(db: AsyncSession, limit: int = 100) -> List[UserTaskRecord]:
    result = await db.execute(
        select(UserTaskRecord)
        .where(
            UserTaskRecord.status.in_(("pending", "running")),
            UserTaskRecord.generation_type.in_(("image", "video", "asset_image_generate", "storyboard_image", "storyboard_video")),
        )
        .order_by(UserTaskRecord.updated_at.asc())
        .limit(limit)
    )
    return [record for record in result.scalars().all() if should_reconcile_provider_task(record)]


def _provider_generation_type(record: UserTaskRecord) -> Optional[str]:
    if record.business_type == "conversation" and record.generation_type in {"image", "video"}:
        return record.generation_type
    if record.business_type == "project" and record.generation_type == "asset_image_generate":
        return "image"
    if record.business_type == "project" and record.generation_type == "storyboard_image":
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


def _video_request_extra(record: UserTaskRecord) -> Dict[str, Any]:
    extra = record.extra or {}
    for key in ("user_message_extra", "model_extra"):
        value = extra.get(key)
        if isinstance(value, dict):
            return value
    return {}


async def _mark_next_reconcile(
    db: AsyncSession,
    record: UserTaskRecord,
    provider_extra: Optional[Dict[str, Any]],
) -> None:
    extra = _clear_provider_reconcile_claim(
        {
            **(record.extra or {}),
            "provider_reconciled_at": beijing_datetime().isoformat(),
            "next_poll_seconds": _provider_reconcile_interval(record),
        }
    )
    if provider_extra is not None:
        extra["last_provider_task_status"] = provider_extra
    record.extra = extra
    await db.commit()


def _should_skip_provider_reconcile(record: UserTaskRecord) -> bool:
    extra = record.extra or {}
    reconciled_at = extra.get("provider_reconciled_at")
    if not reconciled_at:
        return False
    try:
        last_time = datetime.fromisoformat(str(reconciled_at))
    except ValueError:
        return False
    last_time = to_beijing_datetime(last_time)
    elapsed = (beijing_datetime() - last_time).total_seconds()
    return elapsed < _provider_reconcile_interval(record)


def _provider_reconcile_interval(record: Optional[UserTaskRecord]) -> int:
    generation_type = record.generation_type if record is not None else None
    return provider_poll_interval_seconds(generation_type)


def _provider_reconcile_lease_seconds() -> int:
    return max(
        60,
        settings.comfly_timeout_seconds + settings.generated_media_read_timeout_seconds + 30,
    )


def _has_active_provider_reconcile_claim(extra: Dict[str, Any]) -> bool:
    claim_id = extra.get("provider_reconcile_claim_id")
    claim_until = extra.get("provider_reconcile_claim_until")
    if not claim_id or not claim_until:
        return False
    try:
        until = datetime.fromisoformat(str(claim_until))
    except ValueError:
        return False
    until = to_beijing_datetime(until)
    return until > beijing_datetime()


def _clear_provider_reconcile_claim(extra: Dict[str, Any]) -> Dict[str, Any]:
    cleaned = dict(extra)
    cleaned.pop("provider_reconcile_claim_id", None)
    cleaned.pop("provider_reconcile_claimed_at", None)
    cleaned.pop("provider_reconcile_claim_until", None)
    return cleaned


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


async def _refund_interrupted_task_points(db: AsyncSession, record: UserTaskRecord) -> None:
    if record.points_cost <= 0 or (record.extra or {}).get("refund_transaction_id"):
        return
    refund_transaction = await change_user_points(
        db,
        user_id=record.user_id,
        amount=record.points_cost,
        transaction_type="refund",
        remark=f"任务中断退回积分：{record.title}",
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
    if record.generation_type in {
        "character_analysis",
        "scene_analysis",
        "prop_analysis",
        "storyboard_analysis",
        "storyboard_refinement",
        "storyboard_image_prompt",
        "storyboard_prompt_generation",
    }:
        await _sync_chapter_analysis_failed(db, record, reason)
        return
    if record.generation_type == "asset_image_generate":
        await _sync_asset_image_failed(db, record, reason)
        return
    if record.generation_type == "storyboard_image":
        await _sync_storyboard_image_failed(db, record, reason)
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
        "storyboard_refinement": "storyboard_refinement_status",
        "storyboard_image_prompt": "storyboard_image_prompt_generation_status",
        "storyboard_prompt_generation": "storyboard_prompt_generation_status",
    }.get(record.generation_type)
    task_key = {
        "character_analysis": "character_analysis_task_record_id",
        "scene_analysis": "scene_analysis_task_record_id",
        "prop_analysis": "prop_analysis_task_record_id",
        "storyboard_analysis": "storyboard_analysis_task_record_id",
        "storyboard_refinement": "storyboard_refinement_task_record_id",
        "storyboard_image_prompt": "storyboard_image_prompt_generation_task_record_id",
        "storyboard_prompt_generation": "storyboard_prompt_generation_task_record_id",
    }.get(record.generation_type)
    if not status_key:
        return
    chapter.extra = {
        **(chapter.extra or {}),
        status_key: "failed",
        status_key.replace("_status", "_failed_reason"): reason,
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


async def _sync_storyboard_image_failed(db: AsyncSession, record: UserTaskRecord, reason: str) -> None:
    storyboard_id = (record.extra or {}).get("storyboard_id")
    parsed_storyboard_id = _parse_uuid(storyboard_id)
    if parsed_storyboard_id is None:
        return
    storyboard = await db.get(ProjectStoryboard, parsed_storyboard_id)
    if storyboard is None:
        return
    storyboard.extra = {
        **(storyboard.extra or {}),
        "image_generation_status": "failed",
        "image_generation_failed_reason": reason,
        "image_generation_task_record_id": str(record.id),
    }


async def _mark_reconciled_success(
    db: AsyncSession,
    record: UserTaskRecord,
    provider_generation_type: str,
    model_result: ModelRunResult,
) -> None:
    record.status = "success"
    record.result = model_result.content
    record.extra = _clear_provider_reconcile_claim(
        {
            **(record.extra or {}),
            "model_result_extra": model_result.extra,
            "provider_reconciled_at": beijing_datetime().isoformat(),
        }
    )

    if record.business_type == "conversation":
        await _sync_conversation_message_success(db, record, model_result)
    elif record.generation_type == "asset_image_generate":
        await _sync_asset_image_success(db, record, model_result)
    elif record.generation_type == "storyboard_image":
        await _sync_storyboard_image_success(db, record, model_result)
    elif record.generation_type == "storyboard_video":
        await _sync_storyboard_video_success(db, record, model_result)

    if provider_generation_type == "video" and record.ai_model_id:
        ai_model = await db.get(AiModel, record.ai_model_id)
        if ai_model is not None:
            await settle_video_task_points(
                db,
                record,
                ai_model,
                _video_request_extra(record),
                remark_prefix="视频生成",
            )

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
    record.extra = _clear_provider_reconcile_claim(
        {
            **(record.extra or {}),
            "failed_reason": reason,
            "model_result_extra": provider_extra,
            "provider_reconciled_at": beijing_datetime().isoformat(),
        }
    )
    await _refund_failed_task_points(db, record)
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
    elif record.generation_type == "asset_image_generate":
        await _sync_asset_image_failed(db, record, reason)
    elif record.generation_type == "storyboard_image":
        await _sync_storyboard_image_failed(db, record, reason)
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


async def _refund_failed_task_points(db: AsyncSession, record: UserTaskRecord) -> None:
    if record.points_cost <= 0 or (record.extra or {}).get("refund_transaction_id"):
        return
    refund_transaction = await change_user_points(
        db,
        user_id=record.user_id,
        amount=record.points_cost,
        transaction_type="refund",
        remark=f"任务失败退回积分：{record.title}",
        auto_commit=False,
    )
    record.extra = {
        **(record.extra or {}),
        "refund_transaction_id": str(refund_transaction.id),
    }


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
    history = await create_project_generated_asset_history(
        db,
        task_record=record,
        target_type=str(asset_type),
        target_id=parsed_asset_id,
        media_type="image",
        result_urls=extract_result_urls(model_result.content) or [image_url],
        result_url=image_url,
        generation_mode=(record.extra or {}).get("generation_mode"),
        extra={
            "asset_name": (record.extra or {}).get("asset_name"),
            "generation_ratio": (record.extra or {}).get("generation_ratio"),
            "model_result_extra": model_result.extra,
        },
    )
    asset.reference_image = image_url
    asset.updated_at = beijing_datetime()
    asset.extra = {
        **(asset.extra or {}),
        "image_generation_status": "success",
        "image_generation_history_id": str(history.id),
        "image_generation_task_record_id": str(record.id),
        "image_generation_extra": model_result.extra,
    }
    record.result = image_url
    record.extra = {
        **(record.extra or {}),
        "oss_image_url": image_url,
        "generated_asset_history_id": str(history.id),
    }


async def _sync_storyboard_image_success(
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
    image_url = _first_result_url(model_result.content)
    if not image_url:
        return
    result_urls = extract_result_urls(model_result.content) or [image_url]
    history = await create_project_generated_asset_history(
        db,
        task_record=record,
        target_type="storyboard",
        target_id=parsed_storyboard_id,
        media_type="image",
        result_urls=result_urls,
        result_url=image_url,
        chapter_id=storyboard.chapter_id,
        generation_mode="storyboard_image",
        extra={
            "storyboard_title": storyboard.title,
            "shot_number": storyboard.shot_number,
            "aspect_ratio": (record.extra or {}).get("aspect_ratio"),
            "reference_images": (record.extra or {}).get("reference_images"),
            "model_result_extra": model_result.extra,
        },
    )
    storyboard.extra = {
        **(storyboard.extra or {}),
        "image_generation_status": "success",
        "image_generation_history_id": str(history.id),
        "image_generation_task_record_id": str(record.id),
        "image_generation_result": image_url,
        "image_generation_result_urls": result_urls,
        "image_generation_extra": model_result.extra,
    }
    storyboard.updated_at = beijing_datetime()
    record.result = image_url
    record.extra = {
        **(record.extra or {}),
        "oss_image_url": image_url,
        "storyboard_image_result": image_url,
        "generated_asset_history_id": str(history.id),
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
    last_frame_url = _first_generated_last_frame_url(model_result.extra)
    result_urls = extract_result_urls(model_result.content)
    history = await create_project_generated_asset_history(
        db,
        task_record=record,
        target_type="storyboard",
        target_id=parsed_storyboard_id,
        media_type="video",
        result_urls=result_urls or [model_result.content],
        result_url=result_urls[0] if result_urls else model_result.content,
        last_frame_url=last_frame_url,
        chapter_id=storyboard.chapter_id,
        generation_mode=(record.extra or {}).get("generation_mode"),
        extra={
            "storyboard_title": storyboard.title,
            "shot_number": storyboard.shot_number,
            "resolution": (record.extra or {}).get("resolution"),
            "return_last_frame": (record.extra or {}).get("return_last_frame"),
            "model_result_extra": model_result.extra,
        },
    )
    storyboard.extra = {
        **(storyboard.extra or {}),
        "video_generation_status": "success",
        "video_generation_history_id": str(history.id),
        "video_generation_task_record_id": str(record.id),
        "video_generation_result": model_result.content,
        "video_generation_extra": model_result.extra,
        **({"video_generation_last_frame_url": last_frame_url} if last_frame_url else {}),
    }
    storyboard.updated_at = beijing_datetime()
    record.extra = {
        **(record.extra or {}),
        "storyboard_video_result": model_result.content,
        **({"storyboard_video_last_frame_url": last_frame_url} if last_frame_url else {}),
        "generated_asset_history_id": str(history.id),
    }


def _first_result_url(content: str) -> str:
    for value in (content or "").split(","):
        url = value.strip()
        if url.startswith(("http://", "https://")):
            return url
    return ""


def _first_generated_last_frame_url(extra: Dict[str, Any]) -> str:
    for key in ("display_last_frame_urls", "oss_last_frame_urls"):
        value = extra.get(key)
        if isinstance(value, list):
            for item in value:
                if item:
                    return str(item)
        if value:
            return str(value)
    return ""


def _parse_uuid(value: Any) -> Optional[UUID]:
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _is_provider_failed_status(status: str) -> bool:
    return status in {"failed", "failure", "fail", "error", "cancelled", "canceled"}


def _is_provider_success_result(model_result: ModelRunResult, status: str) -> bool:
    if status in {"success", "succeeded", "completed", "complete", "finished", "done"}:
        return True
    if status in {"not_start", "in_progress", "running", "pending", "processing", "queued"}:
        return False
    content = (model_result.content or "").strip()
    return bool(content and content != "生成任务处理中" and not content.startswith("模型任务仍在生成中"))
