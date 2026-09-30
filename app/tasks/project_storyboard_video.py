from app.services.generation.task_events import publish_task_change
import asyncio
from typing import Optional
from uuid import UUID

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select

from app.core.config import settings
from app.core.public_messages import sanitize_public_message
from app.db.session import create_worker_sessionmaker
from app.integrations.model_providers import close_model_provider_clients
from app.models.project_storyboard import ProjectStoryboard
from app.models.task_record import UserTaskRecord
from app.services.billing.model_points import refund_task_points
from app.services.projects.storyboard_videos import run_storyboard_video_generation_in_worker
from app.services.generation.provider_state import has_provider_task_id
from app.services.generation.task_execution import (
    TaskExecutionDeferred,
    lock_active_task,
    prepare_task_execution,
)
from app.tasks.retry_policy import (
    is_retryable_provider_error,
    retry_countdown,
    user_failed_reason,
)
from app.core.celery_app import celery_app


WorkerSessionLocal = create_worker_sessionmaker()


@celery_app.task(
    bind=True,
    name="tasks.project_storyboard_video.run_project_storyboard_video_generation",
    max_retries=settings.celery_task_max_retries,
    soft_time_limit=settings.effective_celery_task_soft_time_limit_seconds,
    time_limit=settings.effective_celery_task_time_limit_seconds,
)
def run_project_storyboard_video_generation(self, task_record_id: str, storyboard_id: str) -> None:
    try:
        asyncio.run(
            _run_project_storyboard_video_generation(UUID(task_record_id), UUID(storyboard_id))
        )
    except SoftTimeLimitExceeded:
        asyncio.run(_fail_generation(UUID(task_record_id), UUID(storyboard_id), "任务执行超时"))
    except TaskExecutionDeferred as exc:
        raise self.retry(countdown=exc.retry_after_seconds) from exc
    except Exception as exc:
        if self.request.retries < settings.celery_task_max_retries and is_retryable_provider_error(
            exc
        ):
            raise self.retry(exc=exc, countdown=retry_countdown(self.request.retries)) from exc
        asyncio.run(
            _fail_generation(
                UUID(task_record_id),
                UUID(storyboard_id),
                user_failed_reason(exc),
                raw_reason=str(exc) or "任务执行失败",
            )
        )


async def _run_project_storyboard_video_generation(
    task_record_id: UUID, storyboard_id: UUID
) -> None:
    try:
        await _execute_generation(task_record_id, storyboard_id)
    finally:
        await close_model_provider_clients()


async def _execute_generation(task_record_id: UUID, storyboard_id: UUID) -> None:
    async with WorkerSessionLocal() as db:
        result = await db.execute(
            select(UserTaskRecord).where(UserTaskRecord.id == task_record_id).with_for_update()
        )
        task_record = result.scalar_one_or_none()
        storyboard = await db.get(ProjectStoryboard, storyboard_id)
        if task_record is None or storyboard is None:
            return
        if not prepare_task_execution(task_record):
            _enqueue_provider_reconcile_if_needed(task_record)
            return

        task_record.status = "running"
        storyboard.extra = {
            **(storyboard.extra or {}),
            "video_generation_status": "running",
            "video_generation_task_record_id": str(task_record.id),
        }
        await db.commit()
        await publish_task_change(task_record)

        try:
            await run_storyboard_video_generation_in_worker(db, task_record, storyboard_id)
        except Exception as exc:
            await db.rollback()
            await db.refresh(task_record)
            if has_provider_task_id(task_record):
                _enqueue_provider_reconcile_if_needed(task_record)
                return
            if is_retryable_provider_error(exc):
                await _mark_retrying(
                    db,
                    task_record,
                    storyboard,
                    sanitize_public_message(str(exc) or "模型服务繁忙，正在重试"),
                )
                raise
            await _mark_failed(
                db,
                task_record,
                storyboard,
                sanitize_public_message(str(exc) or "分镜视频生成失败"),
                refund=True,
                raw_reason=str(exc) or "分镜视频生成失败",
            )
            return
        await db.commit()
        await publish_task_change(task_record)
        _enqueue_provider_reconcile_if_needed(task_record)


async def _fail_generation(
    task_record_id: UUID,
    storyboard_id: UUID,
    reason: str,
    raw_reason: Optional[str] = None,
) -> None:
    async with WorkerSessionLocal() as db:
        task_record = await db.get(UserTaskRecord, task_record_id)
        storyboard = await db.get(ProjectStoryboard, storyboard_id)
        if task_record is None or storyboard is None:
            return
        if task_record.status in {"success", "failed"}:
            return
        if has_provider_task_id(task_record):
            return
        await _mark_failed(db, task_record, storyboard, reason, refund=True, raw_reason=raw_reason)


async def _mark_failed(
    db,
    task_record: UserTaskRecord,
    storyboard: ProjectStoryboard,
    reason: str,
    refund: bool = False,
    raw_reason: Optional[str] = None,
) -> None:
    if not await lock_active_task(db, task_record, allow_provider_task=False):
        return
    await db.refresh(storyboard)
    reason = sanitize_public_message(reason)
    if refund:
        await refund_task_points(db, task_record, remark_prefix="任务失败退回积分")
    refund_transaction_id = (task_record.extra or {}).get("refund_transaction_id")

    task_record.status = "failed"
    task_record.result = reason
    task_record.extra = {
        **(task_record.extra or {}),
        "failed_reason": reason,
        "raw_failed_reason": raw_reason or reason,
        "refund_transaction_id": refund_transaction_id,
    }
    storyboard.extra = {
        **(storyboard.extra or {}),
        "video_generation_status": "failed",
        "video_generation_failed_reason": reason,
        "raw_failed_reason": raw_reason or reason,
        "video_generation_task_record_id": str(task_record.id),
    }
    await db.commit()
    await publish_task_change(task_record)


async def _mark_retrying(
    db, task_record: UserTaskRecord, storyboard: ProjectStoryboard, reason: str
) -> None:
    if not await lock_active_task(db, task_record, allow_provider_task=False):
        return
    await db.refresh(storyboard)
    reason = sanitize_public_message(reason, fallback="模型服务繁忙，正在重试")
    task_record.status = "pending"
    task_record.result = reason
    task_record.extra = {**(task_record.extra or {}), "retry_reason": reason}
    storyboard.extra = {
        **(storyboard.extra or {}),
        "video_generation_status": "pending",
        "video_generation_retry_reason": reason,
        "video_generation_task_record_id": str(task_record.id),
    }
    await db.commit()
    await publish_task_change(task_record)


def _enqueue_provider_reconcile_if_needed(task_record: UserTaskRecord) -> None:
    from app.tasks.provider_reconcile import enqueue_provider_reconcile_best_effort

    enqueue_provider_reconcile_best_effort(task_record)
