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
from app.models.task_record import UserTaskRecord
from app.services.billing.model_points import refund_task_points
from app.services.projects.asset_generation import (
    asset_image_config,
    set_asset_image_generation_state,
    get_task_asset_variant,
    run_asset_image_generation_in_worker,
)
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
    name="tasks.project_asset_generation.run_project_asset_image_generation",
    max_retries=settings.celery_task_max_retries,
    soft_time_limit=settings.effective_celery_task_soft_time_limit_seconds,
    time_limit=settings.effective_celery_task_time_limit_seconds,
)
def run_project_asset_image_generation(
    self, task_record_id: str, asset_type: str, asset_id: str
) -> None:
    try:
        asyncio.run(
            _run_project_asset_image_generation(UUID(task_record_id), asset_type, UUID(asset_id))
        )
    except SoftTimeLimitExceeded:
        asyncio.run(
            _fail_generation(UUID(task_record_id), asset_type, UUID(asset_id), "任务执行超时")
        )
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
                asset_type,
                UUID(asset_id),
                user_failed_reason(exc),
                raw_reason=str(exc) or "任务执行失败",
            )
        )


async def _run_project_asset_image_generation(
    task_record_id: UUID, asset_type: str, asset_id: UUID
) -> None:
    try:
        await _execute_generation(task_record_id, asset_type, asset_id)
    finally:
        await close_model_provider_clients()


async def _execute_generation(task_record_id: UUID, asset_type: str, asset_id: UUID) -> None:
    config = asset_image_config(asset_type)
    async with WorkerSessionLocal() as db:
        result = await db.execute(
            select(UserTaskRecord).where(UserTaskRecord.id == task_record_id).with_for_update()
        )
        task_record = result.scalar_one_or_none()
        asset = await db.get(config["model"], asset_id)
        if task_record is None or asset is None:
            return
        variant = await get_task_asset_variant(db, task_record)
        if not prepare_task_execution(task_record):
            _enqueue_provider_reconcile_if_needed(task_record)
            return

        task_record.status = "running"
        set_asset_image_generation_state(
            asset,
            variant,
            status="running",
            task_record_id=task_record.id,
        )
        await db.commit()
        await publish_task_change(task_record)

        try:
            await run_asset_image_generation_in_worker(db, task_record, asset_type, asset_id)
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
                    asset,
                    variant,
                    sanitize_public_message(str(exc) or "模型服务繁忙，正在重试"),
                )
                raise
            await _mark_failed(
                db,
                task_record,
                asset,
                variant,
                sanitize_public_message(str(exc) or "资产图像生成失败"),
                refund=True,
                raw_reason=str(exc) or "资产图像生成失败",
            )
            return
        await db.commit()
        await publish_task_change(task_record)
        _enqueue_provider_reconcile_if_needed(task_record)


async def _fail_generation(
    task_record_id: UUID,
    asset_type: str,
    asset_id: UUID,
    reason: str,
    raw_reason: Optional[str] = None,
) -> None:
    config = asset_image_config(asset_type)
    async with WorkerSessionLocal() as db:
        task_record = await db.get(UserTaskRecord, task_record_id)
        asset = await db.get(config["model"], asset_id)
        if task_record is None or asset is None:
            return
        variant = await get_task_asset_variant(db, task_record)
        if task_record.status in {"success", "failed"}:
            return
        if has_provider_task_id(task_record):
            return
        await _mark_failed(
            db,
            task_record,
            asset,
            variant,
            reason,
            refund=True,
            raw_reason=raw_reason,
        )


async def _mark_failed(
    db,
    task_record: UserTaskRecord,
    asset,
    variant,
    reason: str,
    refund: bool = False,
    raw_reason: Optional[str] = None,
) -> None:
    if not await lock_active_task(db, task_record, allow_provider_task=False):
        return
    await db.refresh(asset)
    if variant is not None:
        await db.refresh(variant)
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
    set_asset_image_generation_state(
        asset,
        variant,
        status="failed",
        task_record_id=task_record.id,
        failed_reason=reason,
    )
    state = variant.extra if variant is not None else asset.extra
    updated = {**(state or {}), "raw_failed_reason": raw_reason or reason}
    if variant is not None:
        variant.extra = updated
    else:
        asset.extra = updated
    await db.commit()
    await publish_task_change(task_record)


async def _mark_retrying(db, task_record: UserTaskRecord, asset, variant, reason: str) -> None:
    if not await lock_active_task(db, task_record, allow_provider_task=False):
        return
    await db.refresh(asset)
    if variant is not None:
        await db.refresh(variant)
    reason = sanitize_public_message(reason, fallback="模型服务繁忙，正在重试")
    task_record.status = "pending"
    task_record.result = reason
    task_record.extra = {**(task_record.extra or {}), "retry_reason": reason}
    set_asset_image_generation_state(
        asset,
        variant,
        status="pending",
        task_record_id=task_record.id,
    )
    state = variant.extra if variant is not None else asset.extra
    updated = {**(state or {}), "image_generation_retry_reason": reason}
    if variant is not None:
        variant.extra = updated
    else:
        asset.extra = updated
    await db.commit()
    await publish_task_change(task_record)


def _enqueue_provider_reconcile_if_needed(task_record: UserTaskRecord) -> None:
    from app.tasks.provider_reconcile import enqueue_provider_reconcile_best_effort

    enqueue_provider_reconcile_best_effort(task_record)
