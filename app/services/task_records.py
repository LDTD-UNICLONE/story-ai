from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import AppException
from app.core.public_messages import sanitize_public_message
from app.core.timezone import beijing_datetime, to_beijing_datetime
from app.models.ai_model import AiModel
from app.models.agent_story_bible import AgentAssetVariant
from app.models.conversation import ConversationMessage
from app.models.project_asset import ProjectCharacter, ProjectProp, ProjectScene
from app.models.project_chapter import ProjectChapter
from app.models.project_storyboard import ProjectStoryboard
from app.models.task_record import UserTaskRecord
from app.models.user import User
from app.services.generated_media import persist_generated_media_to_oss
from app.services.apimart_private_avatars import (
    complete_private_avatar_review,
    is_private_avatar_stage,
    mark_private_avatar_ready_for_resume,
    update_private_avatar_progress,
)
from app.services.core_asset_change_tracking import track_core_asset_reference_change
from app.services.model_points import (
    build_model_billing_snapshot,
    settle_image_task_points,
    settle_video_task_points,
)
from app.services.model_configuration import ensure_model_available
from app.services.model_runner import ModelRunResult, query_model_task
from app.services.points import change_user_points
from app.services.project_generated_assets import (
    create_project_generated_asset_history,
    extract_result_urls,
    record_storyboard_video_generation_success,
)
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
    {"label": "整剧分块解析", "value": "agent_source_chunk_analysis", "business_type": "project"},
    {"label": "整剧分析合并", "value": "agent_source_global_merge", "business_type": "project"},
    {"label": "整剧资产分析", "value": "agent_asset_analysis", "business_type": "project"},
    {"label": "整剧分集规划", "value": "agent_episode_planning", "business_type": "project"},
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
    expire_stale: bool = True,
) -> UserTaskRecord:
    if expire_stale:
        await expire_stale_task_records(db, user_id=user_id)
    await _lock_user_task_submission(db, user_id)
    queue_snapshot = await _build_user_task_queue_snapshot(db, user_id, generation_type)
    _enforce_user_task_limits(queue_snapshot)
    model_billing_snapshot = None
    if ai_model_id is not None:
        ai_model = await db.get(AiModel, ai_model_id)
        if ai_model is not None:
            ensure_model_available(ai_model)
            model_billing_snapshot = build_model_billing_snapshot(ai_model)
    record_extra = {
        **(extra or {}),
        "queue_snapshot": queue_snapshot,
    }
    if model_billing_snapshot is not None:
        record_extra["model_billing_snapshot"] = model_billing_snapshot
        record_extra["points_settled"] = False
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
        extra=record_extra,
    )
    db.add(record)
    return record


async def _build_user_task_queue_snapshot(
    db: AsyncSession,
    user_id: UUID,
    generation_type: str,
) -> Dict[str, Any]:
    active_since = beijing_datetime() - timedelta(
        hours=max(1, settings.user_pending_task_window_hours)
    )
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
        "limits_block_submission": True,
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
        snapshot["exceeds_configured_task_limit"] = (
            active_tasks_before >= settings.user_pending_task_limit
        )

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


async def _lock_user_task_submission(db: AsyncSession, user_id: UUID) -> None:
    result = await db.execute(select(User).where(User.id == user_id).with_for_update())
    user = result.scalar_one_or_none()
    if user is None:
        raise AppException("用户不存在", code=40401, status_code=404)
    if user.points_balance < 0:
        raise AppException(
            "存在未结清的模型实际费用，请充值后重试",
            code=40003,
            status_code=400,
        )


def _enforce_user_task_limits(snapshot: Dict[str, Any]) -> None:
    if snapshot.get("exceeds_configured_media_task_limit"):
        limit = snapshot.get("configured_media_task_limit")
        raise AppException(f"待处理媒体任务已达到上限（{limit} 个）", code=42920, status_code=429)
    if snapshot.get("exceeds_configured_task_limit"):
        limit = snapshot.get("configured_task_limit")
        raise AppException(f"待处理任务已达到上限（{limit} 个）", code=42920, status_code=429)


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
    conditions = _task_record_filter_conditions(user_id, business_type, generation_type, status)

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


async def list_admin_task_record_summaries(
    db: AsyncSession,
    user_id: Optional[UUID],
    business_type: Optional[str],
    generation_type: Optional[str],
    status: Optional[str],
    page: int,
    page_size: int,
) -> Tuple[List[Dict[str, Any]], int]:
    conditions = _task_record_filter_conditions(user_id, business_type, generation_type, status)

    count_result = await db.execute(
        select(func.count()).select_from(UserTaskRecord).where(*conditions)
    )
    total = count_result.scalar_one()

    result = await db.execute(
        select(
            UserTaskRecord.id,
            UserTaskRecord.user_id,
            UserTaskRecord.ai_model_id,
            UserTaskRecord.points_transaction_id,
            UserTaskRecord.business_type,
            UserTaskRecord.business_id,
            UserTaskRecord.generation_type,
            UserTaskRecord.status,
            UserTaskRecord.title,
            func.substr(UserTaskRecord.prompt, 1, 500).label("prompt_preview"),
            func.substr(UserTaskRecord.result, 1, 500).label("result_preview"),
            UserTaskRecord.points_cost,
            UserTaskRecord.created_at,
            UserTaskRecord.updated_at,
        )
        .where(*conditions)
        .order_by(UserTaskRecord.created_at.desc(), UserTaskRecord.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return [dict(row._mapping) for row in result.all()], total


def _task_record_filter_conditions(
    user_id: Optional[UUID],
    business_type: Optional[str],
    generation_type: Optional[str],
    status: Optional[str],
) -> List[Any]:
    conditions: List[Any] = []
    if user_id:
        conditions.append(UserTaskRecord.user_id == user_id)
    if business_type:
        conditions.append(UserTaskRecord.business_type == business_type)
    if generation_type:
        conditions.append(UserTaskRecord.generation_type == generation_type)
    if status:
        conditions.append(UserTaskRecord.status == status)
    return conditions


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
    records = [
        records_by_id[task_record_id]
        for task_record_id in unique_ids
        if task_record_id in records_by_id
    ]
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
        select(UserTaskRecord).where(UserTaskRecord.id == task_record_id).with_for_update()
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


async def cancel_project_task_records(
    db: AsyncSession,
    project_id: UUID,
    user_id: UUID,
    *,
    reason: str,
) -> int:
    """在删除项目的同一事务中中止其活动任务并退回已扣积分。"""
    result = await db.execute(
        select(UserTaskRecord)
        .where(
            UserTaskRecord.business_type == "project",
            UserTaskRecord.business_id == project_id,
            UserTaskRecord.user_id == user_id,
            UserTaskRecord.status.in_(("pending", "running")),
        )
        .with_for_update()
    )
    records = list(result.scalars().all())
    await _cancel_task_records_without_commit(
        db,
        records,
        reason=reason,
        markers={"project_deleted": True},
    )
    return len(records)


async def cancel_project_resource_task_records(
    db: AsyncSession,
    project_id: UUID,
    user_id: UUID,
    *,
    match_extra: Dict[str, Any],
    reason: str,
) -> int:
    """中止项目内与指定章节、资产或分镜关联的活动任务。"""
    result = await db.execute(
        select(UserTaskRecord)
        .where(
            UserTaskRecord.business_type == "project",
            UserTaskRecord.business_id == project_id,
            UserTaskRecord.user_id == user_id,
            UserTaskRecord.status.in_(("pending", "running")),
        )
        .with_for_update()
    )
    expected = {key: str(value) for key, value in match_extra.items()}
    records = [
        record
        for record in result.scalars().all()
        if all(str((record.extra or {}).get(key)) == value for key, value in expected.items())
    ]
    await _cancel_task_records_without_commit(
        db,
        records,
        reason=reason,
        markers={"resource_deleted": True},
    )
    return len(records)


async def _cancel_task_records_without_commit(
    db: AsyncSession,
    records: List[UserTaskRecord],
    *,
    reason: str,
    markers: Dict[str, Any],
) -> None:
    public_reason = sanitize_public_message(reason)
    interrupted_at = beijing_datetime().isoformat()
    for record in records:
        record.status = "failed"
        record.result = public_reason
        record.extra = {
            **(record.extra or {}),
            "failed_reason": public_reason,
            "interrupted": True,
            **markers,
            "interrupted_at": interrupted_at,
        }
        await _refund_interrupted_task_points(db, record)
        await _sync_stale_failed_business_state(db, record, public_reason)


async def reconcile_provider_task_record(
    db: AsyncSession, task_record_id: UUID
) -> Optional[UserTaskRecord]:
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
            model_result = await persist_generated_media_to_oss(
                claim.provider_generation_type, model_result
            )
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


async def _claim_provider_reconcile(
    db: AsyncSession, task_record_id: UUID
) -> Optional[ProviderReconcileClaim]:
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

    task_id = getattr(record, "provider_task_id", None) or _extract_provider_task_id(
        record.extra or {}
    )
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
    record.provider_task_id = task_id
    record.provider_vendor = record.provider_vendor or ai_model.vendor
    record.provider_status = record.provider_status or "submitted"
    record.provider_submitted_at = record.provider_submitted_at or record.updated_at or now
    record.last_reconcile_at = now
    record.next_reconcile_at = now + timedelta(seconds=_provider_reconcile_lease_seconds())
    record.reconcile_attempts = int(record.reconcile_attempts or 0) + 1
    record.extra = {
        **(record.extra or {}),
        "provider_reconcile_claim_id": claim_id,
        "provider_reconcile_claimed_at": now.isoformat(),
        "provider_reconcile_claim_until": (
            now + timedelta(seconds=_provider_reconcile_lease_seconds())
        ).isoformat(),
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
    if is_private_avatar_stage(record):
        if not _is_provider_terminal_status(status):
            update_private_avatar_progress(record, model_result.extra)
            await _mark_next_reconcile(db, record, None)
            await db.refresh(record)
            return record

        private_avatar_result = await complete_private_avatar_review(
            db,
            record,
            model_result.extra,
        )
        if private_avatar_result.failed_reason:
            await _mark_reconciled_failed(
                db,
                record,
                private_avatar_result.failed_reason,
                model_result.extra,
            )
        else:
            mark_private_avatar_ready_for_resume(record)
            record.extra = _clear_provider_reconcile_claim(record.extra or {})
            await db.commit()
            await db.refresh(record)
        return record

    if _is_provider_failed_status(status):
        await _mark_reconciled_failed(
            db, record, f"模型任务执行失败：{status or 'failed'}", model_result.extra
        )
    elif not _is_provider_success_result(model_result, status):
        now = beijing_datetime()
        record_provider_task_state(
            record,
            model_result.extra,
            provider_vendor=claim.ai_model.vendor,
        )
        record.last_reconcile_at = now
        record.extra = _clear_provider_reconcile_claim(
            {
                **(record.extra or {}),
                "last_provider_task_status": model_result.extra,
                "provider_reconciled_at": now.isoformat(),
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
        and has_provider_task_id(record)
        and not _has_active_provider_reconcile_claim(record.extra or {})
    )


def provider_reconcile_delay_seconds(record: Optional[UserTaskRecord] = None) -> int:
    if record is not None:
        next_poll_seconds = (record.extra or {}).get("next_poll_seconds")
        if isinstance(next_poll_seconds, int) and next_poll_seconds > 0:
            return next_poll_seconds
        return _provider_reconcile_interval(record)
    return _provider_reconcile_interval(None)


async def list_provider_reconcile_candidates(
    db: AsyncSession, limit: int = 100
) -> List[UserTaskRecord]:
    now = beijing_datetime()
    result = await db.execute(
        select(UserTaskRecord)
        .where(
            UserTaskRecord.status.in_(("pending", "running")),
            UserTaskRecord.generation_type.in_(
                ("image", "video", "asset_image_generate", "storyboard_image", "storyboard_video")
            ),
            UserTaskRecord.provider_task_id.is_not(None),
            or_(
                UserTaskRecord.next_reconcile_at.is_(None),
                UserTaskRecord.next_reconcile_at <= now,
            ),
        )
        .order_by(UserTaskRecord.next_reconcile_at.asc().nullsfirst(), UserTaskRecord.id.asc())
        .limit(limit)
    )
    records = list(result.scalars().all())
    remaining = max(0, limit - len(records))
    if remaining:
        legacy_result = await db.execute(
            select(UserTaskRecord)
            .where(
                UserTaskRecord.status.in_(("pending", "running")),
                UserTaskRecord.generation_type.in_(
                    (
                        "image",
                        "video",
                        "asset_image_generate",
                        "storyboard_image",
                        "storyboard_video",
                    )
                ),
                UserTaskRecord.provider_task_id.is_(None),
                _provider_task_id_sql_condition(),
            )
            .order_by(UserTaskRecord.updated_at.asc())
            .limit(remaining)
        )
        records.extend(legacy_result.scalars().all())
    return [record for record in records if should_reconcile_provider_task(record)]


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


def record_provider_task_state(
    record: UserTaskRecord,
    provider_extra: Dict[str, Any],
    provider_vendor: Optional[str] = None,
) -> bool:
    task_id = _extract_provider_task_id(provider_extra)
    if not task_id:
        return False

    now = beijing_datetime()
    provider_status = _extract_provider_status(provider_extra)
    if not provider_status:
        provider_status = str(getattr(record, "status", "") or "submitted").lower()

    record.provider_task_id = task_id
    if provider_vendor:
        record.provider_vendor = provider_vendor
    if getattr(record, "provider_submitted_at", None) is None:
        record.provider_submitted_at = now
    record.provider_status = provider_status
    progress_percent = _extract_provider_progress_percent(provider_extra)
    if _is_provider_success_status(provider_status):
        progress_percent = 100
    if progress_percent is not None:
        record.extra = {
            **(getattr(record, "extra", None) or {}),
            "progress_percent": progress_percent,
        }
    if _is_provider_terminal_status(provider_status):
        record.next_reconcile_at = None
    else:
        record.next_reconcile_at = now + timedelta(seconds=_provider_reconcile_interval(record))
    return True


def task_record_progress_percent(record: UserTaskRecord) -> Optional[int]:
    generation_type = str(getattr(record, "generation_type", "") or "")
    if generation_type not in {
        "image",
        "video",
        "asset_image_generate",
        "storyboard_image",
        "storyboard_video",
    }:
        return None
    if str(getattr(record, "status", "") or "").lower() == "success":
        return 100

    extra = getattr(record, "extra", None) or {}
    if is_private_avatar_stage(record):
        private_avatar = extra.get("private_avatar") or {}
        progress_percent = _normalize_progress_percent(
            private_avatar.get("progress_percent")
        )
        if progress_percent is not None:
            return progress_percent
    for candidate in (
        extra,
        extra.get("last_provider_task_status"),
        extra.get("model_result_extra"),
        extra.get("assistant_message_extra"),
    ):
        progress_percent = _extract_provider_progress_percent(candidate)
        if progress_percent is not None:
            return progress_percent
    return None


def _extract_provider_progress_percent(value: Any) -> Optional[int]:
    if not isinstance(value, dict):
        return None

    candidates = [value]
    provider_response = value.get("provider_response")
    if isinstance(provider_response, dict):
        candidates.append(provider_response)
        data = provider_response.get("data")
        if isinstance(data, dict):
            candidates.append(data)
        elif isinstance(data, list) and data and isinstance(data[0], dict):
            candidates.append(data[0])

    for candidate in candidates:
        raw_progress = candidate.get("progress_percent")
        if raw_progress is None:
            raw_progress = candidate.get("progress")
        progress_percent = _normalize_progress_percent(raw_progress)
        if progress_percent is not None:
            return progress_percent
    return None


def _normalize_progress_percent(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        progress_percent = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return None
    if not 0 <= progress_percent <= 100:
        return None
    return progress_percent


def _extract_provider_status(extra: Dict[str, Any]) -> str:
    value = extra.get("task_status") or extra.get("platform_task_status")
    return str(value or "").strip().lower()


def _is_provider_terminal_status(status: str) -> bool:
    return status in {
        "success",
        "succeeded",
        "completed",
        "complete",
        "finished",
        "done",
        "failed",
        "failure",
        "fail",
        "error",
        "cancelled",
        "canceled",
    }


def _is_provider_success_status(status: str) -> bool:
    return status in {"success", "succeeded", "completed", "complete", "finished", "done"}


def has_provider_task_id(record: UserTaskRecord) -> bool:
    return bool(
        getattr(record, "provider_task_id", None) or _extract_provider_task_id(record.extra or {})
    )


def _provider_task_id_sql_condition():
    extra = UserTaskRecord.extra
    return or_(
        extra["task_id"].as_string().is_not(None),
        extra["provider_task_id"].as_string().is_not(None),
        extra["model_result_extra"]["task_id"].as_string().is_not(None),
        extra["assistant_message_extra"]["task_id"].as_string().is_not(None),
        extra["last_provider_task_status"]["task_id"].as_string().is_not(None),
        extra["last_provider_task_status"]["taskId"].as_string().is_not(None),
    )


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
    now = beijing_datetime()
    record.last_reconcile_at = now
    record.next_reconcile_at = now + timedelta(seconds=_provider_reconcile_interval(record))
    if provider_extra is not None:
        provider_status = _extract_provider_status(provider_extra)
        if provider_status:
            record.provider_status = provider_status
    extra = _clear_provider_reconcile_claim(
        {
            **(record.extra or {}),
            "provider_reconciled_at": now.isoformat(),
            "next_poll_seconds": _provider_reconcile_interval(record),
        }
    )
    if provider_extra is not None:
        extra["last_provider_task_status"] = provider_extra
    record.extra = extra
    await db.commit()


def _should_skip_provider_reconcile(record: UserTaskRecord) -> bool:
    next_reconcile_at = getattr(record, "next_reconcile_at", None)
    if next_reconcile_at is not None:
        return to_beijing_datetime(next_reconcile_at) > beijing_datetime()
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
    if record is not None and is_private_avatar_stage(record):
        return max(5, settings.provider_task_poll_interval_seconds)
    generation_type = record.generation_type if record is not None else None
    return provider_poll_interval_seconds(generation_type)


def _provider_reconcile_lease_seconds() -> int:
    return max(
        60,
        settings.provider_request_timeout_ceiling_seconds
        + settings.generated_media_read_timeout_seconds
        + 30,
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
        UserTaskRecord.provider_task_id.is_(None),
        ~_provider_task_id_sql_condition(),
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
    expired_count = 0
    for record in records:
        if has_provider_task_id(record):
            continue
        await _mark_stale_failed(db, record)
        expired_count += 1
    return expired_count


async def expire_stale_task_record(db: AsyncSession, record: UserTaskRecord) -> bool:
    if record.status not in {"pending", "running"}:
        return False
    if has_provider_task_id(record):
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
    await _refund_task_points(db, record, remark_prefix="任务超时失败退回积分")
    await _sync_stale_failed_business_state(db, record, reason)
    await db.commit()


async def _refund_interrupted_task_points(db: AsyncSession, record: UserTaskRecord) -> None:
    await _refund_task_points(db, record, remark_prefix="任务中断退回积分")


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
    result = await db.execute(
        select(ConversationMessage).where(
            ConversationMessage.id == parsed_assistant_message_id,
            ConversationMessage.user_id == record.user_id,
            ConversationMessage.conversation_id == record.business_id,
            ConversationMessage.role == "assistant",
        )
    )
    assistant_message = result.scalar_one_or_none()
    if assistant_message is None:
        return
    assistant_message.content = f"任务执行失败：{reason}"
    assistant_message.extra = {
        **(assistant_message.extra or {}),
        "task_status": "failed",
        "failed_reason": reason,
        "task_record_id": str(record.id),
    }
    if assistant_message.message_type == "text":
        assistant_message.status = "failed"


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


async def _sync_chapter_analysis_failed(
    db: AsyncSession, record: UserTaskRecord, reason: str
) -> None:
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
    variant_id = _parse_uuid((record.extra or {}).get("agent_asset_variant_id"))
    if variant_id is not None:
        variant = await db.get(AgentAssetVariant, variant_id)
        if variant is not None:
            variant.extra = {
                **(variant.extra or {}),
                "image_generation_status": "failed",
                "image_generation_failed_reason": reason,
                "image_generation_task_record_id": str(record.id),
            }
            variant.updated_at = beijing_datetime()
        return
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


async def _sync_storyboard_video_failed(
    db: AsyncSession, record: UserTaskRecord, reason: str
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
        "video_generation_status": "failed",
        "video_generation_failed_reason": reason,
        "video_generation_task_record_id": str(record.id),
    }


async def _sync_storyboard_image_failed(
    db: AsyncSession, record: UserTaskRecord, reason: str
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
    now = beijing_datetime()
    record.status = "success"
    record.result = model_result.content
    record.provider_status = _extract_provider_status(model_result.extra) or "success"
    record.last_reconcile_at = now
    record.next_reconcile_at = None
    record.extra = _clear_provider_reconcile_claim(
        {
            **(record.extra or {}),
            "progress_percent": 100,
            "model_result_extra": model_result.extra,
            "provider_reconciled_at": now.isoformat(),
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

    if provider_generation_type in {"image", "video"} and record.ai_model_id:
        ai_model = await db.get(AiModel, record.ai_model_id)
        if ai_model is not None:
            if provider_generation_type == "image":
                await settle_image_task_points(
                    db,
                    record,
                    ai_model,
                    model_result.extra,
                    remark_prefix="图像生成",
                )
            else:
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
    now = beijing_datetime()
    record.status = "failed"
    record.result = reason
    record.provider_status = _extract_provider_status(provider_extra) or "failed"
    record.last_reconcile_at = now
    record.next_reconcile_at = None
    reconciled_extra = {
        **(record.extra or {}),
        "failed_reason": reason,
        "model_result_extra": provider_extra,
        "provider_reconciled_at": now.isoformat(),
    }
    progress_percent = _extract_provider_progress_percent(provider_extra)
    if progress_percent is not None:
        reconciled_extra["progress_percent"] = progress_percent
    record.extra = _clear_provider_reconcile_claim(reconciled_extra)
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
    await _refund_task_points(db, record, remark_prefix="任务失败退回积分")


async def _refund_task_points(
    db: AsyncSession,
    record: UserTaskRecord,
    *,
    remark_prefix: str,
) -> None:
    if record.points_cost <= 0 or (record.extra or {}).get("refund_transaction_id"):
        return
    refund_transaction = await change_user_points(
        db,
        user_id=record.user_id,
        amount=record.points_cost,
        transaction_type="refund",
        remark=f"{remark_prefix}：{record.title}",
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
    variant_id = _parse_uuid((record.extra or {}).get("agent_asset_variant_id"))
    if variant_id is not None:
        variant = await db.get(AgentAssetVariant, variant_id)
        image_url = _first_result_url(model_result.content)
        if variant is None or not image_url:
            return
        asset_id = _parse_uuid((record.extra or {}).get("asset_id"))
        asset_type = str((record.extra or {}).get("asset_type") or "")
        if (
            record.business_id is not None
            and asset_id is not None
            and asset_type in {"character", "scene", "prop"}
        ):
            await track_core_asset_reference_change(
                db,
                project_id=record.business_id,
                user_id=record.user_id,
                asset_type=asset_type,
                asset_id=asset_id,
                variant_id=variant.id,
                previous_reference_image=variant.reference_image,
                new_reference_image=image_url,
                source="worker",
            )
        history = await create_project_generated_asset_history(
            db,
            task_record=record,
            target_type="asset_variant",
            target_id=variant.id,
            media_type="image",
            result_urls=extract_result_urls(model_result.content) or [image_url],
            result_url=image_url,
            generation_mode=(record.extra or {}).get("generation_mode"),
            extra={
                "variant_name": variant.canonical_name,
                "generation_ratio": (record.extra or {}).get("generation_ratio"),
                "model_result_extra": model_result.extra,
            },
        )
        variant.reference_image = image_url
        variant.lock_version += 1
        variant.updated_at = beijing_datetime()
        variant.extra = {
            **(variant.extra or {}),
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
        return
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
    await track_core_asset_reference_change(
        db,
        project_id=asset.project_id,
        user_id=asset.user_id,
        asset_type=str(asset_type),
        asset_id=asset.id,
        previous_reference_image=asset.reference_image,
        new_reference_image=image_url,
        source="worker",
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
    await record_storyboard_video_generation_success(
        db,
        task_record=record,
        storyboard=storyboard,
        content=model_result.content,
        result_extra=model_result.extra,
        last_frame_url=last_frame_url,
    )


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
    if status in {
        "not_start",
        "submitted",
        "in_progress",
        "running",
        "pending",
        "processing",
        "queued",
    }:
        return False
    content = (model_result.content or "").strip()
    return bool(
        content and content != "生成任务处理中" and not content.startswith("模型任务仍在生成中")
    )
