import asyncio
import logging
from types import SimpleNamespace
from typing import Optional
from uuid import UUID

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select

from app.core.config import settings
from app.core.exceptions import AppException
from app.core.logging import log_extra
from app.core.public_messages import sanitize_public_message
from app.core.timezone import beijing_datetime
from app.db.session import create_worker_sessionmaker
from app.integrations.comfly import close_comfly_client
from app.integrations.volcengine_ark import close_volcengine_ark_client
from app.models.ai_model import AiModel
from app.models.project_chapter import ProjectChapter
from app.models.task_record import UserTaskRecord
from app.services.model_points import settle_text_task_points
from app.services.model_runner import run_model
from app.services.points import change_user_points
from app.services.prompts import render_system_prompt
from app.services.task_records import refresh_task_record_interrupted
from app.services.task_execution import TaskExecutionDeferred, prepare_task_execution
from app.worker import celery_app


WorkerSessionLocal = create_worker_sessionmaker()
logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    name="tasks.project_chapter.run_project_chapter_processing",
    max_retries=settings.celery_task_max_retries,
    soft_time_limit=settings.effective_celery_task_soft_time_limit_seconds,
    time_limit=settings.effective_celery_task_time_limit_seconds,
)
def run_project_chapter_processing(self, task_record_id: str, chapter_id: str) -> None:
    try:
        asyncio.run(_run_project_chapter_processing(UUID(task_record_id), UUID(chapter_id)))
    except (SoftTimeLimitExceeded, asyncio.TimeoutError):
        asyncio.run(
            _fail_processing(
                UUID(task_record_id), UUID(chapter_id), "任务执行超时", raw_reason="任务执行超时"
            )
        )
    except TaskExecutionDeferred as exc:
        raise self.retry(countdown=exc.retry_after_seconds) from exc
    except Exception as exc:
        if self.request.retries < settings.celery_task_max_retries and _is_retryable_provider_error(
            exc
        ):
            raise self.retry(exc=exc, countdown=_retry_countdown(self.request.retries)) from exc
        asyncio.run(
            _fail_processing(
                UUID(task_record_id),
                UUID(chapter_id),
                _user_failed_reason(exc),
                raw_reason=str(exc) or "任务执行失败",
            )
        )


async def _run_project_chapter_processing(task_record_id: UUID, chapter_id: UUID) -> None:
    try:
        await asyncio.wait_for(
            _execute_processing(task_record_id, chapter_id),
            timeout=_chapter_processing_timeout_seconds(),
        )
    finally:
        await close_comfly_client()
        await close_volcengine_ark_client()


def _chapter_processing_timeout_seconds() -> int:
    return max(
        1,
        settings.effective_celery_task_soft_time_limit_seconds
        - min(10, max(1, settings.celery_task_timeout_grace_seconds)),
    )


async def _execute_processing(task_record_id: UUID, chapter_id: UUID) -> None:
    async with WorkerSessionLocal() as db:
        result = await db.execute(
            select(UserTaskRecord).where(UserTaskRecord.id == task_record_id).with_for_update()
        )
        task_record = result.scalar_one_or_none()
        chapter = await db.get(ProjectChapter, chapter_id)
        if task_record is None or chapter is None:
            return
        if not prepare_task_execution(task_record):
            return

        task_record.status = "running"
        chapter.process_status = "running"
        await db.commit()

        result = await db.execute(
            select(AiModel).where(
                AiModel.id == task_record.ai_model_id,
                AiModel.model_type == "text",
                AiModel.is_enabled.is_(True),
            )
        )
        ai_model = result.scalar_one_or_none()
        if ai_model is None:
            await _mark_failed(db, task_record, chapter, "文本模型不存在或已禁用", refund=True)
            return

        model_snapshot = SimpleNamespace(
            id=ai_model.id,
            model_id=ai_model.model_id,
            vendor=ai_model.vendor,
            nickname=ai_model.nickname,
            points_cost=ai_model.points_cost,
            capabilities=ai_model.capabilities or {},
        )
        try:
            model_prompt = _resolve_model_prompt(task_record, chapter)
            task_record.extra = {
                **(task_record.extra or {}),
                "provider_call_status": "started",
                "provider_call_started_at": beijing_datetime().isoformat(),
                "provider_vendor": ai_model.vendor,
                "provider_model_id": ai_model.model_id,
            }
            await db.commit()
            logger.info(
                "Project chapter provider call started",
                extra=log_extra(
                    event="project_chapter_provider_call_started",
                    task_record_id=task_record.id,
                    chapter_id=chapter.id,
                    model_id=ai_model.model_id,
                    vendor=ai_model.vendor,
                ),
            )
            model_result = await run_model(
                model_snapshot,
                "text",
                model_prompt,
                (task_record.extra or {}).get("model_extra") or {},
                idempotency_key=str(task_record.id),
            )
            task_record.extra = {
                **(task_record.extra or {}),
                "provider_call_status": "finished",
                "provider_call_finished_at": beijing_datetime().isoformat(),
            }
        except Exception as exc:
            if _is_retryable_provider_error(exc):
                await _mark_retrying(
                    db,
                    task_record,
                    chapter,
                    sanitize_public_message(str(exc) or "模型服务繁忙，正在重试"),
                )
                raise
            await _mark_failed(
                db,
                task_record,
                chapter,
                sanitize_public_message(str(exc) or "模型调用失败"),
                refund=True,
                raw_reason=str(exc) or "模型调用失败",
            )
            return

        if await refresh_task_record_interrupted(db, task_record):
            return

        await settle_text_task_points(
            db,
            task_record,
            ai_model,
            model_result.extra,
            remark_prefix="章节文本处理",
        )

        chapter.processed_content = model_result.content
        chapter.process_status = "success"
        chapter.updated_at = beijing_datetime()
        chapter.extra = {
            **(chapter.extra or {}),
            "task_record_id": str(task_record.id),
            "model_extra": model_result.extra,
        }
        task_record.status = "success"
        task_record.result = model_result.content
        if (task_record.extra or {}).get("prompt_source") == "system":
            task_record.prompt = "系统提示词"
        task_record.extra = {
            **(task_record.extra or {}),
            "model_result_extra": model_result.extra,
            "chapter_id": str(chapter.id),
        }
        await db.commit()


async def _fail_processing(
    task_record_id: UUID,
    chapter_id: UUID,
    reason: str,
    raw_reason: Optional[str] = None,
) -> None:
    async with WorkerSessionLocal() as db:
        task_record = await db.get(UserTaskRecord, task_record_id)
        chapter = await db.get(ProjectChapter, chapter_id)
        if task_record is None or chapter is None:
            return
        if task_record.status in {"success", "failed"}:
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
        **(task_record.extra or {}),
        "failed_reason": reason,
        "raw_failed_reason": raw_reason or reason,
        "refund_transaction_id": refund_transaction_id,
    }
    chapter.process_status = "failed"
    chapter.extra = {
        **(chapter.extra or {}),
        "failed_reason": reason,
        "raw_failed_reason": raw_reason or reason,
        "task_record_id": str(task_record.id),
    }
    await db.commit()


async def _mark_retrying(
    db,
    task_record: UserTaskRecord,
    chapter: ProjectChapter,
    reason: str,
) -> None:
    reason = sanitize_public_message(reason, fallback="模型服务繁忙，正在重试")
    task_record.status = "pending"
    task_record.result = reason
    task_record.extra = {
        **(task_record.extra or {}),
        "retry_reason": reason,
    }
    chapter.process_status = "pending"
    chapter.extra = {
        **(chapter.extra or {}),
        "retry_reason": reason,
        "task_record_id": str(task_record.id),
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


def _resolve_model_prompt(task_record: UserTaskRecord, chapter: ProjectChapter) -> str:
    if not (chapter.content or "").strip():
        raise AppException("章节原文内容不能为空", code=40036, status_code=400)
    if (task_record.extra or {}).get("prompt_source") == "system":
        prompt = render_system_prompt("chapter_text_cleaning.md", input_text=chapter.content)
        if "{{input_text}}" in prompt:
            raise AppException("章节原文未正确写入模型提示词", code=50042, status_code=500)
        return prompt
    return task_record.prompt


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
        return "模型服务繁忙，请稍后再试"
    return sanitize_public_message(str(exc) or "任务执行失败")
