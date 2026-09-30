"""Create, query, interrupt and expire platform task records.

Creation (including stale cleanup) and project/resource cancellation join the caller's
transaction. Admin interruption and standalone stale expiration commit their own
task, refund and business changes.
"""

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
from app.models.task_record import UserTaskRecord
from app.models.user import User
from app.services.models.configuration import ensure_model_available
from app.services.billing.model_points import build_model_billing_snapshot, refund_task_points
from app.services.generation.provider_state import has_provider_task_id, provider_task_id_sql_condition
from app.services.generation.business_results import sync_task_business_failure


TASK_RECORD_BUSINESS_TYPES = [
    {"label": "对话型", "value": "conversation"},
    {"label": "项目型", "value": "project"},
]

TASK_RECORD_GENERATION_TYPES = [
    {"label": "画布文本生成", "value": "text", "business_type": "project"},
    {"label": "画布图像生成", "value": "image", "business_type": "project"},
    {"label": "画布视频生成", "value": "video", "business_type": "project"},
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
        await expire_stale_task_records(db, user_id=user_id, auto_commit=False)
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
    await refund_task_points(db, record, remark_prefix="任务中断退回积分")
    await sync_task_business_failure(db, record, public_reason)
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
        await refund_task_points(db, record, remark_prefix="任务中断退回积分")
        await sync_task_business_failure(db, record, public_reason)


async def expire_stale_task_records(
    db: AsyncSession,
    user_id: Optional[UUID] = None,
    business_type: Optional[str] = None,
    generation_type: Optional[str] = None,
    limit: int = 100,
    *,
    auto_commit: bool = True,
) -> int:
    conditions = [
        UserTaskRecord.status.in_(("pending", "running")),
        UserTaskRecord.created_at <= _stale_task_cutoff(),
        UserTaskRecord.provider_task_id.is_(None),
        ~provider_task_id_sql_condition(),
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
        if await _mark_stale_failed(db, record, auto_commit=auto_commit):
            expired_count += 1
    return expired_count


async def expire_stale_task_record(db: AsyncSession, record: UserTaskRecord) -> bool:
    if record.status not in {"pending", "running"}:
        return False
    if has_provider_task_id(record):
        return False
    if record.created_at > _stale_task_cutoff():
        return False
    if not await _mark_stale_failed(db, record):
        return False
    await db.refresh(record)
    return True


def _stale_task_cutoff() -> datetime:
    timeout_minutes = max(1, settings.task_stale_timeout_minutes)
    return beijing_datetime() - timedelta(minutes=timeout_minutes)


async def _mark_stale_failed(
    db: AsyncSession,
    record: UserTaskRecord,
    *,
    auto_commit: bool = True,
) -> bool:
    # Refresh under the task lock: another worker may have finished or obtained a provider ID.
    record = await db.scalar(
        select(UserTaskRecord)
        .where(UserTaskRecord.id == record.id)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    if (
        record is None
        or record.status not in {"pending", "running"}
        or has_provider_task_id(record)
        or record.created_at > _stale_task_cutoff()
    ):
        return False
    reason = f"任务超过 {max(1, settings.task_stale_timeout_minutes)} 分钟未完成，已自动判定失败"
    record.status = "failed"
    record.result = reason
    record.extra = {
        **(record.extra or {}),
        "failed_reason": reason,
        "stale_failed": True,
        "stale_failed_at": beijing_datetime().isoformat(),
    }
    await refund_task_points(db, record, remark_prefix="任务超时失败退回积分")
    await sync_task_business_failure(db, record, reason)
    if auto_commit:
        await db.commit()
    return True
