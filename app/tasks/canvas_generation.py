"""Execute frozen canvas requests using existing leases, billing and provider recovery."""

from app.services.generation.task_events import publish_task_change

import asyncio
from uuid import UUID

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select

from app.core.celery_app import celery_app
from app.core.config import settings
from app.core.exceptions import AppException
from app.core.public_messages import sanitize_public_message
from app.db.session import create_worker_sessionmaker
from app.integrations.model_providers import close_model_provider_clients
from app.models.task_record import UserTaskRecord
from app.services.billing.model_points import refund_task_points
from app.services.generation.media import persist_generated_media_to_oss
from app.services.generation.runner import ModelRunResult, run_model
from app.services.generation.provider_state import (
    extract_provider_task_id,
    has_provider_task_id,
    record_provider_task_state,
)
from app.services.generation.task_execution import (
    TaskExecutionDeferred,
    lock_active_task,
    prepare_task_execution,
)
from app.services.projects.canvas_results import runtime_model, save_result, settle_completed
from app.services.seedance_images import provider_scope
from app.tasks.retry_policy import is_retryable_provider_error, retry_countdown, user_failed_reason
from app.tasks import provider_reconcile

WorkerSessionLocal = create_worker_sessionmaker()


@celery_app.task(
    bind=True,
    name="tasks.canvas_generation.run_canvas_generation",
    max_retries=settings.celery_task_max_retries,
)
def run_canvas_generation(self, task_record_id):
    task_id = UUID(task_record_id)
    try:
        asyncio.run(execute_generation(task_id))
    except TaskExecutionDeferred as exc:
        raise self.retry(countdown=exc.retry_after_seconds) from exc
    except SoftTimeLimitExceeded:
        asyncio.run(fail_generation(task_id, "任务执行超时"))
    except Exception as exc:
        if self.request.retries < settings.celery_task_max_retries and is_retryable_provider_error(
            exc
        ):
            raise self.retry(exc=exc, countdown=retry_countdown(self.request.retries)) from exc
        asyncio.run(fail_generation(task_id, user_failed_reason(exc)))


async def execute_generation(task_id):
    try:
        await _execute(task_id)
    finally:
        await close_model_provider_clients()


async def _execute(task_id):
    async with WorkerSessionLocal() as db:
        task = await db.scalar(
            select(UserTaskRecord).where(UserTaskRecord.id == task_id).with_for_update()
        )
        if task is None or not task.extra.get("canvas_generation_id"):
            return
        if task.status == "success":
            await db.commit()
            await publish_task_change(task)
            await settle_completed(db, task_id)
            return
        if not prepare_task_execution(task):
            provider_reconcile.enqueue_provider_reconcile_best_effort(task)
            return
        task.status = "running"
        await db.commit()
        await publish_task_change(task)
        try:
            model = runtime_model(task)
            cached = task.extra.get("canvas_raw_result")
            if cached:
                result = ModelRunResult(**cached)
            else:
                scope = task.extra["canvas_snapshot"].get("seedance_provider_scope")
                if scope and scope != provider_scope():
                    raise AppException("素材供应商配置已变更，请重新审核并提交", code=40016)
                result = await run_model(
                    model,
                    task.generation_type,
                    task.prompt,
                    task.extra["model_extra"],
                    idempotency_key=str(task.id),
                )
                if not await lock_active_task(db, task):
                    return
                record_provider_task_state(task, result.extra, model.vendor)
                task.extra = {
                    **task.extra,
                    "canvas_raw_result": {"content": result.content, "extra": result.extra},
                    "model_result_extra": result.extra,
                }
                await db.commit()
                await publish_task_change(task)
            if extract_provider_task_id(result.extra):
                # The common reconciler owns polling; do not occupy a media worker while waiting.
                provider_reconcile.enqueue_provider_reconcile_best_effort(task)
                return
            result = await persist_generated_media_to_oss(task.generation_type, result)
            if not await lock_active_task(db, task):
                return
            await save_result(db, task, result)
            task.status, task.result = "success", result.content
            task.extra = {**task.extra, "model_result_extra": result.extra}
            await db.commit()
            await publish_task_change(task)
        except Exception:
            await db.rollback()
            if await lock_active_task(db, task, allow_provider_task=False):
                task.status = "pending"
                await db.commit()
                await publish_task_change(task)
                raise
            # An accepted provider job is reconciled, never resubmitted/refunded here.
            return
        await settle_completed(db, task_id)


async def fail_generation(task_id, reason):
    async with WorkerSessionLocal() as db:
        task = await db.get(UserTaskRecord, task_id)
        if task is None or not task.extra.get("canvas_generation_id") or has_provider_task_id(task):
            return
        if not await lock_active_task(db, task, allow_provider_task=False):
            return
        await refund_task_points(db, task, remark_prefix="任务失败退回积分")
        task.status = "failed"
        task.result = sanitize_public_message(reason)
        task.extra = {**task.extra, "failed_reason": task.result}
        await db.commit()
        await publish_task_change(task)


@celery_app.task(name="tasks.canvas_generation.settle_pending_canvas_points")
def settle_pending_canvas_points(limit=100):
    return asyncio.run(_settle_pending(max(1, limit)))


async def _settle_pending(limit):
    async with WorkerSessionLocal() as db:
        ids = list(
            await db.scalars(
                select(UserTaskRecord.id)
                .where(
                    UserTaskRecord.business_type == "project",
                    UserTaskRecord.status == "success",
                    UserTaskRecord.extra["canvas_generation_id"].as_string().is_not(None),
                    UserTaskRecord.extra["points_settled"].as_boolean().is_(False),
                )
                .order_by(UserTaskRecord.updated_at, UserTaskRecord.id)
                .limit(limit)
            )
        )
    count = 0
    for task_id in ids:
        async with WorkerSessionLocal() as db:
            count += int(await settle_completed(db, task_id))
    return count
