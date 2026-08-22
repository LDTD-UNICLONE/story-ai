import asyncio
from typing import Optional
from uuid import UUID

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select

from app.core.config import settings
from app.core.exceptions import AppException
from app.core.public_messages import sanitize_public_message
from app.db.session import create_worker_sessionmaker
from app.integrations.model_providers import close_model_provider_clients
from app.models.project_storyboard import ProjectStoryboard
from app.models.task_record import UserTaskRecord
from app.services.points import change_user_points
from app.services.project_storyboard_images import run_storyboard_image_generation_in_worker
from app.services.task_records import has_provider_task_id
from app.services.task_execution import TaskExecutionDeferred, prepare_task_execution
from app.worker import celery_app


WorkerSessionLocal = create_worker_sessionmaker()


@celery_app.task(
    bind=True,
    name="tasks.project_storyboard_image.run_project_storyboard_image_generation",
    max_retries=settings.celery_task_max_retries,
    soft_time_limit=settings.effective_celery_task_soft_time_limit_seconds,
    time_limit=settings.effective_celery_task_time_limit_seconds,
)
def run_project_storyboard_image_generation(self, task_record_id: str, storyboard_id: str) -> None:
    try:
        asyncio.run(
            _run_project_storyboard_image_generation(UUID(task_record_id), UUID(storyboard_id))
        )
    except SoftTimeLimitExceeded:
        asyncio.run(_fail_generation(UUID(task_record_id), UUID(storyboard_id), "任务执行超时"))
    except TaskExecutionDeferred as exc:
        raise self.retry(countdown=exc.retry_after_seconds) from exc
    except Exception as exc:
        if self.request.retries < settings.celery_task_max_retries and _is_retryable_provider_error(
            exc
        ):
            raise self.retry(exc=exc, countdown=_retry_countdown(self.request.retries)) from exc
        asyncio.run(
            _fail_generation(
                UUID(task_record_id),
                UUID(storyboard_id),
                _user_failed_reason(exc),
                raw_reason=str(exc) or "任务执行失败",
            )
        )


async def _run_project_storyboard_image_generation(
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
            return

        task_record.status = "running"
        storyboard.extra = {
            **(storyboard.extra or {}),
            "image_generation_status": "running",
            "image_generation_task_record_id": str(task_record.id),
        }
        await db.commit()

        try:
            await run_storyboard_image_generation_in_worker(db, task_record, storyboard_id)
        except Exception as exc:
            if has_provider_task_id(task_record):
                await db.rollback()
                await db.refresh(task_record)
                if has_provider_task_id(task_record):
                    _enqueue_provider_reconcile_if_needed(task_record)
                    return
            if _is_retryable_provider_error(exc):
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
                sanitize_public_message(str(exc) or "分镜故事板图像生成失败"),
                refund=True,
                raw_reason=str(exc) or "分镜故事板图像生成失败",
            )
            return
        await db.commit()
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
    reason = sanitize_public_message(reason)
    refund_transaction_id = (task_record.extra or {}).get("refund_transaction_id")
    already_refunded = bool(refund_transaction_id)
    if refund and task_record.points_cost > 0 and not already_refunded:
        refund_transaction = await change_user_points(
            db,
            user_id=task_record.user_id,
            amount=task_record.points_cost,
            transaction_type="refund",
            remark=f"任务失败退回积分：{task_record.title}",
            auto_commit=False,
        )
        refund_transaction_id = str(refund_transaction.id)

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
        "image_generation_status": "failed",
        "image_generation_failed_reason": reason,
        "raw_failed_reason": raw_reason or reason,
        "image_generation_task_record_id": str(task_record.id),
    }
    await db.commit()


async def _mark_retrying(
    db, task_record: UserTaskRecord, storyboard: ProjectStoryboard, reason: str
) -> None:
    reason = sanitize_public_message(reason, fallback="模型服务繁忙，正在重试")
    task_record.status = "pending"
    task_record.result = reason
    task_record.extra = {**(task_record.extra or {}), "retry_reason": reason}
    storyboard.extra = {
        **(storyboard.extra or {}),
        "image_generation_status": "pending",
        "image_generation_retry_reason": reason,
        "image_generation_task_record_id": str(task_record.id),
    }
    await db.commit()


def _is_retryable_provider_error(exc: Exception) -> bool:
    if isinstance(exc, AppException):
        if exc.code == 50231 or exc.status_code in {400, 401, 403}:
            return False
        if _is_non_retryable_provider_error_text(str(exc)):
            return False
        return exc.status_code >= 500 or exc.code in {50202, 50206}
    return False


def _is_non_retryable_provider_error_text(message: str) -> bool:
    normalized = message.lower()
    non_retryable_tokens = (
        "http 400",
        "badrequest",
        "invalidparameter",
        "sensitivecontentdetected",
        "privacyinformation",
        "real person",
        "not valid",
        "content policy",
    )
    return any(token in normalized for token in non_retryable_tokens)


def _retry_countdown(retries: int) -> int:
    countdown = settings.celery_task_retry_countdown_seconds * (2**retries)
    return min(countdown, settings.celery_task_retry_backoff_max_seconds)


def _user_failed_reason(exc: Exception) -> str:
    if _is_retryable_provider_error(exc):
        return "模型服务繁忙，已自动重试多次仍未成功，请稍后再试"
    return sanitize_public_message(str(exc) or "任务执行失败")


def _enqueue_provider_reconcile_if_needed(task_record: UserTaskRecord) -> None:
    from app.tasks.provider_reconcile import enqueue_provider_reconcile_best_effort

    enqueue_provider_reconcile_best_effort(task_record)
