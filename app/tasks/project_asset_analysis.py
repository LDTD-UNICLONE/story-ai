import asyncio
from typing import Optional
from uuid import UUID

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select

from app.core.config import settings
from app.core.exceptions import AppException
from app.core.public_messages import sanitize_public_message
from app.db.session import create_worker_sessionmaker
from app.integrations.comfly import close_comfly_client
from app.integrations.volcengine_ark import close_volcengine_ark_client
from app.models.project_chapter import ProjectChapter
from app.models.task_record import UserTaskRecord
from app.services.points import change_user_points
from app.services.project_asset_analysis import _asset_config, run_asset_analysis_in_worker
from app.worker import celery_app


WorkerSessionLocal = create_worker_sessionmaker()


@celery_app.task(
    bind=True,
    name="tasks.project_asset_analysis.run_project_asset_analysis",
    max_retries=settings.celery_task_max_retries,
    soft_time_limit=settings.celery_task_soft_time_limit_seconds,
    time_limit=settings.celery_task_time_limit_seconds,
)
def run_project_asset_analysis(self, task_record_id: str, chapter_id: str, asset_type: str) -> None:
    try:
        asyncio.run(_run_project_asset_analysis(UUID(task_record_id), UUID(chapter_id), asset_type))
    except SoftTimeLimitExceeded:
        asyncio.run(_fail_analysis(UUID(task_record_id), UUID(chapter_id), asset_type, "任务执行超时"))
    except Exception as exc:
        if self.request.retries < settings.celery_task_max_retries and _is_retryable_provider_error(exc):
            raise self.retry(exc=exc, countdown=_retry_countdown(self.request.retries)) from exc
        asyncio.run(
            _fail_analysis(
                UUID(task_record_id),
                UUID(chapter_id),
                asset_type,
                _user_failed_reason(exc),
                raw_reason=str(exc) or "任务执行失败",
            )
        )


async def _run_project_asset_analysis(task_record_id: UUID, chapter_id: UUID, asset_type: str) -> None:
    try:
        await _execute_analysis(task_record_id, chapter_id, asset_type)
    finally:
        await close_comfly_client()
        await close_volcengine_ark_client()


async def _execute_analysis(task_record_id: UUID, chapter_id: UUID, asset_type: str) -> None:
    config = _asset_config(asset_type)
    async with WorkerSessionLocal() as db:
        result = await db.execute(
            select(UserTaskRecord)
            .where(UserTaskRecord.id == task_record_id)
            .with_for_update(skip_locked=True)
        )
        task_record = result.scalar_one_or_none()
        chapter = await db.get(ProjectChapter, chapter_id)
        if task_record is None or chapter is None:
            return
        if task_record.status != "pending":
            return

        task_record.status = "running"
        chapter.extra = {
            **(chapter.extra or {}),
            config["status_key"]: "running",
            config["task_key"]: str(task_record.id),
        }
        await db.commit()

        try:
            await run_asset_analysis_in_worker(db, task_record, chapter, asset_type)
        except Exception as exc:
            if _is_retryable_provider_error(exc):
                await _mark_retrying(
                    db,
                    task_record,
                    chapter,
                    config,
                    sanitize_public_message(str(exc) or "模型服务繁忙，正在重试"),
                )
                raise
            await _mark_failed(
                db,
                task_record,
                chapter,
                config,
                sanitize_public_message(str(exc) or "资源分析失败"),
                refund=True,
                raw_reason=str(exc) or "资源分析失败",
            )
            return
        await db.commit()


async def _fail_analysis(
    task_record_id: UUID,
    chapter_id: UUID,
    asset_type: str,
    reason: str,
    raw_reason: Optional[str] = None,
) -> None:
    config = _asset_config(asset_type)
    async with WorkerSessionLocal() as db:
        task_record = await db.get(UserTaskRecord, task_record_id)
        chapter = await db.get(ProjectChapter, chapter_id)
        if task_record is None or chapter is None:
            return
        if task_record.status in {"success", "failed"}:
            return
        await _mark_failed(db, task_record, chapter, config, reason, refund=True, raw_reason=raw_reason)


async def _mark_failed(
    db,
    task_record: UserTaskRecord,
    chapter: ProjectChapter,
    config: dict,
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
    chapter.extra = {
        **(chapter.extra or {}),
        config["status_key"]: "failed",
        f"{config['status_key']}_failed_reason": reason,
        "raw_failed_reason": raw_reason or reason,
        config["task_key"]: str(task_record.id),
    }
    await db.commit()


async def _mark_retrying(db, task_record: UserTaskRecord, chapter: ProjectChapter, config: dict, reason: str) -> None:
    reason = sanitize_public_message(reason, fallback="模型服务繁忙，正在重试")
    task_record.status = "pending"
    task_record.result = reason
    task_record.extra = {**(task_record.extra or {}), "retry_reason": reason}
    chapter.extra = {
        **(chapter.extra or {}),
        config["status_key"]: "pending",
        f"{config['status_key']}_retry_reason": reason,
        config["task_key"]: str(task_record.id),
    }
    await db.commit()


def _is_retryable_provider_error(exc: Exception) -> bool:
    if isinstance(exc, AppException):
        if _is_non_retryable_provider_error_text(str(exc)):
            return False
        return exc.status_code >= 500 or exc.code in {50202, 50204, 50206}
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
