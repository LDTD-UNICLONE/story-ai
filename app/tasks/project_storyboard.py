import asyncio
import logging
from typing import Optional
from uuid import UUID

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select

from app.core.config import settings
from app.core.exceptions import AppException
from app.core.logging import log_extra
from app.core.public_messages import sanitize_public_message
from app.db.session import create_worker_sessionmaker
from app.integrations.model_providers import close_model_provider_clients
from app.models.project_chapter import ProjectChapter
from app.models.project_storyboard import ProjectStoryboard
from app.models.task_record import UserTaskRecord
from app.services.points import change_user_points
from app.services.project_storyboards import (
    mark_storyboard_analysis_task_superseded,
    run_storyboard_analysis_in_worker,
    run_storyboard_stage_in_worker,
    storyboard_analysis_task_is_current,
)
from app.services.task_execution import TaskExecutionDeferred, prepare_task_execution
from app.worker import celery_app


WorkerSessionLocal = create_worker_sessionmaker()
logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    name="tasks.project_storyboard.run_project_storyboard_analysis",
    max_retries=settings.celery_task_max_retries,
    soft_time_limit=settings.effective_celery_task_soft_time_limit_seconds,
    time_limit=settings.effective_celery_task_time_limit_seconds,
)
def run_project_storyboard_analysis(self, task_record_id: str, chapter_id: str) -> None:
    retry_delay = _retry_countdown(self.request.retries)
    try:
        asyncio.run(
            _run_project_storyboard_analysis(UUID(task_record_id), UUID(chapter_id), retry_delay)
        )
    except SoftTimeLimitExceeded:
        asyncio.run(_fail_analysis(UUID(task_record_id), UUID(chapter_id), "任务执行超时"))
    except TaskExecutionDeferred as exc:
        raise self.retry(countdown=exc.retry_after_seconds) from exc
    except Exception as exc:
        if self.request.retries < settings.celery_task_max_retries and _is_retryable_provider_error(
            exc
        ):
            raise self.retry(exc=exc, countdown=retry_delay) from exc
        asyncio.run(
            _fail_analysis(
                UUID(task_record_id),
                UUID(chapter_id),
                _user_failed_reason(exc),
                raw_reason=str(exc) or "任务执行失败",
            )
        )


@celery_app.task(
    bind=True,
    name="tasks.project_storyboard.run_project_storyboard_stage",
    max_retries=settings.celery_task_max_retries,
    soft_time_limit=settings.effective_celery_task_soft_time_limit_seconds,
    time_limit=settings.effective_celery_task_time_limit_seconds,
)
def run_project_storyboard_stage(self, task_record_id: str, chapter_id: str) -> None:
    retry_delay = _retry_countdown(self.request.retries)
    try:
        asyncio.run(
            _run_project_storyboard_stage(UUID(task_record_id), UUID(chapter_id), retry_delay)
        )
    except SoftTimeLimitExceeded:
        asyncio.run(_fail_analysis(UUID(task_record_id), UUID(chapter_id), "任务执行超时"))
    except TaskExecutionDeferred as exc:
        raise self.retry(countdown=exc.retry_after_seconds) from exc
    except Exception as exc:
        if self.request.retries < settings.celery_task_max_retries and _is_retryable_provider_error(
            exc
        ):
            raise self.retry(exc=exc, countdown=retry_delay) from exc
        asyncio.run(
            _fail_analysis(
                UUID(task_record_id),
                UUID(chapter_id),
                _user_failed_reason(exc),
                raw_reason=str(exc) or "任务执行失败",
            )
        )


async def _run_project_storyboard_analysis(
    task_record_id: UUID, chapter_id: UUID, retry_delay: int
) -> None:
    try:
        await _execute_analysis(task_record_id, chapter_id, retry_delay)
    finally:
        await close_model_provider_clients()


async def _run_project_storyboard_stage(
    task_record_id: UUID, chapter_id: UUID, retry_delay: int
) -> None:
    try:
        await _execute_stage(task_record_id, chapter_id, retry_delay)
    finally:
        await close_model_provider_clients()


async def _execute_analysis(task_record_id: UUID, chapter_id: UUID, retry_delay: int) -> None:
    async with WorkerSessionLocal() as db:
        result = await db.execute(
            select(UserTaskRecord).where(UserTaskRecord.id == task_record_id).with_for_update()
        )
        task_record = result.scalar_one_or_none()
        chapter = await db.get(ProjectChapter, chapter_id, with_for_update=True)
        if task_record is None or chapter is None:
            logger.warning(
                "Storyboard analysis skipped: task_record or chapter missing",
                extra=log_extra(
                    event="storyboard_analysis_skipped",
                    task_record_id=task_record_id,
                    chapter_id=chapter_id,
                    reason="missing_task_or_chapter",
                ),
            )
            return
        if not prepare_task_execution(task_record):
            logger.info(
                "Storyboard analysis skipped: task_record is not pending",
                extra=log_extra(
                    event="storyboard_analysis_skipped",
                    task_record_id=task_record.id,
                    chapter_id=chapter.id,
                    status=task_record.status,
                    reason="not_executable",
                ),
            )
            return
        if not storyboard_analysis_task_is_current(task_record, chapter):
            await mark_storyboard_analysis_task_superseded(db, task_record)
            await db.commit()
            return

        task_record.status = "running"
        chapter.extra = {
            **(chapter.extra or {}),
            "storyboard_analysis_status": "running",
            "storyboard_analysis_task_record_id": str(task_record.id),
        }
        await db.commit()

        try:
            await run_storyboard_analysis_in_worker(db, task_record, chapter)
        except Exception as exc:
            await db.refresh(chapter, with_for_update=True)
            if not storyboard_analysis_task_is_current(task_record, chapter):
                await mark_storyboard_analysis_task_superseded(db, task_record)
                await db.commit()
                return
            if _is_retryable_provider_error(exc):
                await _mark_retrying(
                    db,
                    task_record,
                    chapter,
                    sanitize_public_message(str(exc) or "模型服务繁忙，正在重试"),
                    retry_delay,
                )
                raise
            await _mark_failed(
                db,
                task_record,
                chapter,
                sanitize_public_message(str(exc) or "分镜分析失败"),
                refund=True,
                raw_reason=str(exc) or "分镜分析失败",
            )
            return
        await db.commit()
        await db.refresh(task_record)
        logger.info(
            "Storyboard analysis business task committed",
            extra=log_extra(
                event="storyboard_analysis_committed",
                task_record_id=task_record.id,
                chapter_id=chapter.id,
                status=task_record.status,
                storyboard_count=(task_record.extra or {}).get("storyboard_count"),
            ),
        )


async def _execute_stage(task_record_id: UUID, chapter_id: UUID, retry_delay: int) -> None:
    async with WorkerSessionLocal() as db:
        result = await db.execute(
            select(UserTaskRecord).where(UserTaskRecord.id == task_record_id).with_for_update()
        )
        task_record = result.scalar_one_or_none()
        chapter = await db.get(ProjectChapter, chapter_id)
        if task_record is None or chapter is None:
            logger.warning(
                "Storyboard stage skipped: task_record or chapter missing",
                extra=log_extra(
                    event="storyboard_stage_skipped",
                    task_record_id=task_record_id,
                    chapter_id=chapter_id,
                    reason="missing_task_or_chapter",
                ),
            )
            return
        if not prepare_task_execution(task_record):
            logger.info(
                "Storyboard stage skipped: task_record is not pending",
                extra=log_extra(
                    event="storyboard_stage_skipped",
                    task_record_id=task_record.id,
                    chapter_id=chapter.id,
                    status=task_record.status,
                    reason="not_executable",
                ),
            )
            return

        status_key, task_key = _stage_keys(task_record.generation_type)
        task_record.status = "running"
        chapter.extra = {
            **(chapter.extra or {}),
            status_key: "running",
            task_key: str(task_record.id),
        }
        storyboard = await _get_task_storyboard(db, task_record)
        if storyboard is not None:
            storyboard.extra = {
                **(storyboard.extra or {}),
                status_key: "running",
                task_key: str(task_record.id),
            }
        await db.commit()

        try:
            await run_storyboard_stage_in_worker(db, task_record, chapter)
        except Exception as exc:
            if _is_retryable_provider_error(exc):
                await _mark_retrying(
                    db,
                    task_record,
                    chapter,
                    sanitize_public_message(str(exc) or "模型服务繁忙，正在重试"),
                    retry_delay,
                )
                raise
            await _mark_failed(
                db,
                task_record,
                chapter,
                sanitize_public_message(str(exc) or "分镜阶段任务失败"),
                refund=True,
                raw_reason=str(exc) or "分镜阶段任务失败",
            )
            return
        await db.commit()
        await db.refresh(task_record)
        logger.info(
            "Storyboard stage business task committed",
            extra=log_extra(
                event="storyboard_stage_committed",
                task_record_id=task_record.id,
                chapter_id=chapter.id,
                generation_type=task_record.generation_type,
                status=task_record.status,
                storyboard_count=(task_record.extra or {}).get("storyboard_count"),
            ),
        )


async def _fail_analysis(
    task_record_id: UUID,
    chapter_id: UUID,
    reason: str,
    raw_reason: Optional[str] = None,
) -> None:
    async with WorkerSessionLocal() as db:
        task_record = await db.get(UserTaskRecord, task_record_id)
        chapter = await db.get(ProjectChapter, chapter_id, with_for_update=True)
        if task_record is None or chapter is None:
            return
        if task_record.status in {"success", "failed"}:
            return
        if not storyboard_analysis_task_is_current(task_record, chapter):
            await mark_storyboard_analysis_task_superseded(db, task_record)
            await db.commit()
            return
        await _mark_failed(db, task_record, chapter, reason, refund=True, raw_reason=raw_reason)


async def _mark_failed(
    db,
    task_record: UserTaskRecord,
    chapter: ProjectChapter,
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
        **_clear_task_retry_state(task_record.extra or {}),
        "failed_reason": reason,
        "raw_failed_reason": raw_reason or reason,
        "refund_transaction_id": refund_transaction_id,
    }
    status_key, task_key = _stage_keys(task_record.generation_type)
    chapter.extra = {
        **_clear_status_retry_state(chapter.extra or {}, status_key),
        status_key: "failed",
        status_key.replace("_status", "_failed_reason"): reason,
        "raw_failed_reason": raw_reason or reason,
        task_key: str(task_record.id),
    }
    storyboard = await _get_task_storyboard(db, task_record)
    if storyboard is not None:
        storyboard.extra = {
            **_clear_status_retry_state(storyboard.extra or {}, status_key),
            status_key: "failed",
            status_key.replace("_status", "_failed_reason"): reason,
            task_key: str(task_record.id),
        }
    await db.commit()
    logger.warning(
        "Storyboard business task marked failed",
        extra=log_extra(
            event="storyboard_task_failed",
            task_record_id=task_record.id,
            chapter_id=chapter.id,
            generation_type=task_record.generation_type,
            reason=reason,
            raw_reason=raw_reason or reason,
        ),
    )


async def _mark_retrying(
    db,
    task_record: UserTaskRecord,
    chapter: ProjectChapter,
    reason: str,
    next_poll_seconds: int,
) -> None:
    reason = sanitize_public_message(reason, fallback="模型服务繁忙，正在重试")
    task_record.status = "pending"
    task_record.result = reason
    task_record.extra = {
        **(task_record.extra or {}),
        "retry_reason": reason,
        "next_poll_seconds": max(1, next_poll_seconds),
    }
    status_key, task_key = _stage_keys(task_record.generation_type)
    chapter.extra = {
        **(chapter.extra or {}),
        status_key: "pending",
        status_key.replace("_status", "_retry_reason"): reason,
        task_key: str(task_record.id),
    }
    storyboard = await _get_task_storyboard(db, task_record)
    if storyboard is not None:
        storyboard.extra = {
            **(storyboard.extra or {}),
            status_key: "pending",
            status_key.replace("_status", "_retry_reason"): reason,
            task_key: str(task_record.id),
        }
    await db.commit()
    logger.info(
        "Storyboard business task scheduled for retry",
        extra=log_extra(
            event="storyboard_task_retrying",
            task_record_id=task_record.id,
            chapter_id=chapter.id,
            generation_type=task_record.generation_type,
            reason=reason,
            next_poll_seconds=max(1, next_poll_seconds),
        ),
    )


async def _get_task_storyboard(db, task_record: UserTaskRecord) -> Optional[ProjectStoryboard]:
    storyboard_id = (task_record.extra or {}).get("storyboard_id")
    if not storyboard_id:
        return None
    try:
        parsed_storyboard_id = UUID(str(storyboard_id))
    except ValueError:
        return None
    return await db.get(ProjectStoryboard, parsed_storyboard_id)


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


def _stage_keys(generation_type: str) -> tuple[str, str]:
    if generation_type == "storyboard_refinement":
        return "storyboard_refinement_status", "storyboard_refinement_task_record_id"
    if generation_type in {"storyboard_image_prompt", "storyboard_image_prompt_generation"}:
        return (
            "storyboard_image_prompt_generation_status",
            "storyboard_image_prompt_generation_task_record_id",
        )
    if generation_type == "storyboard_prompt_generation":
        return "storyboard_prompt_generation_status", "storyboard_prompt_generation_task_record_id"
    return "storyboard_analysis_status", "storyboard_analysis_task_record_id"


def _clear_task_retry_state(extra: dict) -> dict:
    cleaned = dict(extra)
    cleaned.pop("retry_reason", None)
    cleaned.pop("next_poll_seconds", None)
    return cleaned


def _clear_status_retry_state(extra: dict, status_key: str) -> dict:
    cleaned = dict(extra)
    cleaned.pop(status_key.replace("_status", "_retry_reason"), None)
    return cleaned
