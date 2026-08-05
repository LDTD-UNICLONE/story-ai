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
from app.models.conversation import Conversation, ConversationMessage
from app.models.task_record import UserTaskRecord
from app.services.generated_media import persist_generated_media_to_oss
from app.services.model_points import settle_text_task_points, settle_video_task_points
from app.services.model_runner import ModelRunResult, query_model_task, run_model
from app.services.points import change_user_points
from app.services.provider_polling import provider_poll_interval_seconds
from app.services.task_records import (
    has_provider_task_id,
    record_provider_task_state,
    refresh_task_record_interrupted,
)
from app.services.task_execution import TaskExecutionDeferred, prepare_task_execution
from app.worker import celery_app


WorkerSessionLocal = create_worker_sessionmaker()
logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    name="tasks.model_generation.run_conversation_generation",
    max_retries=settings.celery_task_max_retries,
    soft_time_limit=settings.effective_celery_task_soft_time_limit_seconds,
    time_limit=settings.effective_celery_task_time_limit_seconds,
)
def run_conversation_generation(self, task_record_id: str, assistant_message_id: str) -> None:
    try:
        asyncio.run(_run_conversation_generation(UUID(task_record_id), UUID(assistant_message_id)))
    except SoftTimeLimitExceeded:
        asyncio.run(
            _fail_generation(UUID(task_record_id), UUID(assistant_message_id), "任务执行超时")
        )
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
                UUID(assistant_message_id),
                _user_failed_reason(exc),
                raw_reason=str(exc) or "任务执行失败",
            )
        )


async def _run_conversation_generation(task_record_id: UUID, assistant_message_id: UUID) -> None:
    try:
        await _execute_generation(task_record_id, assistant_message_id)
    finally:
        await close_comfly_client()
        await close_volcengine_ark_client()


async def _fail_generation(
    task_record_id: UUID,
    assistant_message_id: UUID,
    reason: str,
    raw_reason: Optional[str] = None,
) -> None:
    async with WorkerSessionLocal() as db:
        task_record = await db.get(UserTaskRecord, task_record_id)
        assistant_message = await db.get(ConversationMessage, assistant_message_id)
        if task_record is None or assistant_message is None:
            return
        if task_record.status in {"success", "failed"}:
            return
        if has_provider_task_id(task_record):
            return
        await _mark_failed(
            db, task_record, assistant_message, reason, refund=True, raw_reason=raw_reason
        )


async def _execute_generation(task_record_id: UUID, assistant_message_id: UUID) -> None:
    async with WorkerSessionLocal() as db:
        result = await db.execute(
            select(UserTaskRecord).where(UserTaskRecord.id == task_record_id).with_for_update()
        )
        task_record = result.scalar_one_or_none()
        assistant_message = await db.get(ConversationMessage, assistant_message_id)
        if task_record is None or assistant_message is None:
            logger.warning(
                "Conversation generation skipped: task_record or assistant message missing",
                extra=log_extra(
                    event="conversation_generation_skipped",
                    task_record_id=task_record_id,
                    assistant_message_id=assistant_message_id,
                    reason="missing_task_or_message",
                ),
            )
            return
        if not prepare_task_execution(task_record):
            logger.info(
                "Conversation generation skipped: task_record is not pending",
                extra=log_extra(
                    event="conversation_generation_skipped",
                    task_record_id=task_record.id,
                    assistant_message_id=assistant_message.id,
                    status=task_record.status,
                    reason="not_executable",
                ),
            )
            return

        task_record.status = "running"
        assistant_message.extra = {**(assistant_message.extra or {}), "task_status": "running"}
        if assistant_message.message_type == "text":
            assistant_message.status = "running"
        await db.commit()
        logger.info(
            "Conversation generation business task running",
            extra=log_extra(
                event="conversation_generation_running",
                task_record_id=task_record.id,
                assistant_message_id=assistant_message.id,
                generation_type=task_record.generation_type,
                ai_model_id=task_record.ai_model_id,
            ),
        )

        result = await db.execute(
            select(AiModel).where(
                AiModel.id == task_record.ai_model_id,
                AiModel.is_enabled.is_(True),
            )
        )
        ai_model = result.scalar_one_or_none()
        if ai_model is None:
            await _mark_failed(
                db, task_record, assistant_message, "模型不存在或已禁用", refund=True
            )
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
            model_result = await run_model(
                model_snapshot,
                task_record.generation_type,
                task_record.prompt,
                (task_record.extra or {}).get("user_message_extra") or {},
                idempotency_key=str(task_record.id),
            )
            if record_provider_task_state(task_record, model_result.extra, ai_model.vendor):
                task_record.extra = {
                    **(task_record.extra or {}),
                    "assistant_message_extra": model_result.extra,
                    "assistant_message_id": str(assistant_message.id),
                }
                await db.commit()
            model_result = await _resolve_provider_task_result(
                model_snapshot,
                task_record.generation_type,
                model_result,
            )
            model_result = await persist_generated_media_to_oss(
                task_record.generation_type, model_result
            )
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
                    assistant_message,
                    sanitize_public_message(str(exc) or "模型服务繁忙，正在重试"),
                )
                raise
            await _mark_failed(
                db,
                task_record,
                assistant_message,
                sanitize_public_message(str(exc) or "模型调用失败"),
                refund=True,
                raw_reason=str(exc) or "模型调用失败",
                failure_extra=_failure_extra(exc),
            )
            return

        if await refresh_task_record_interrupted(db, task_record):
            return

        resolved_status = model_result.extra.get("platform_task_status") or "success"
        assistant_message.content = model_result.content
        assistant_message.extra = {
            **(assistant_message.extra or {}),
            **model_result.extra,
            "task_status": resolved_status,
            "task_record_id": str(task_record.id),
        }
        if assistant_message.message_type == "text":
            assistant_message.status = resolved_status
        task_record.status = resolved_status
        task_record.result = model_result.content
        task_record.extra = {
            **(task_record.extra or {}),
            "assistant_message_extra": model_result.extra,
            "assistant_message_id": str(assistant_message.id),
        }
        record_provider_task_state(task_record, model_result.extra, ai_model.vendor)
        if task_record.business_id:
            await db.execute(
                Conversation.__table__.update()
                .where(Conversation.id == task_record.business_id)
                .values(updated_at=beijing_datetime())
            )
        await db.commit()
        logger.info(
            "Conversation generation business task committed",
            extra=log_extra(
                event="conversation_generation_committed",
                task_record_id=task_record.id,
                assistant_message_id=assistant_message.id,
                generation_type=task_record.generation_type,
                status=task_record.status,
                result_preview=(task_record.result or "")[:200],
            ),
        )

        if task_record.generation_type == "text":
            await _settle_text_points_after_success(
                db, task_record.id, ai_model, model_result.extra
            )
        elif task_record.generation_type == "video":
            await _settle_video_points_after_success(db, task_record.id, ai_model)
        _enqueue_provider_reconcile_if_needed(task_record)


async def _mark_failed(
    db,
    task_record: UserTaskRecord,
    assistant_message: ConversationMessage,
    reason: str,
    refund: bool = False,
    raw_reason: Optional[str] = None,
    failure_extra: Optional[dict] = None,
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
        **(failure_extra or {}),
        "failed_reason": reason,
        "display_message": f"任务执行失败：{reason}",
        "raw_failed_reason": raw_reason or reason,
        "refund_transaction_id": refund_transaction_id,
    }
    assistant_message.content = f"任务执行失败：{reason}"
    assistant_message.extra = {
        **(assistant_message.extra or {}),
        "task_status": "failed",
        "failed_reason": reason,
        "display_message": f"任务执行失败：{reason}",
        "task_record_id": str(task_record.id),
    }
    if assistant_message.message_type == "text":
        assistant_message.status = "failed"
    await db.commit()
    logger.warning(
        "Conversation generation business task failed",
        extra=log_extra(
            event="conversation_generation_failed",
            task_record_id=task_record.id,
            assistant_message_id=assistant_message.id,
            generation_type=task_record.generation_type,
            reason=reason,
            raw_reason=raw_reason or reason,
        ),
    )


def _failure_extra(exc: Exception) -> dict:
    provider_response_summary = getattr(exc, "provider_response_summary", None)
    if provider_response_summary:
        return {"provider_response_summary": provider_response_summary}
    return {}


async def _settle_text_points_after_success(
    db,
    task_record_id: UUID,
    ai_model: AiModel,
    model_result_extra: dict,
) -> None:
    task_record = await db.get(UserTaskRecord, task_record_id)
    if task_record is None or task_record.status != "success":
        return
    try:
        await settle_text_task_points(
            db,
            task_record,
            ai_model,
            model_result_extra,
            remark_prefix="对话模型调用",
        )
        await db.commit()
    except Exception:
        await db.rollback()
        task_record = await db.get(UserTaskRecord, task_record_id)
        if task_record is None or task_record.status != "success":
            return
        task_record.extra = {
            **(task_record.extra or {}),
            "points_settlement_failed": "积分结算失败，已保留生成结果",
        }
        await db.commit()


async def _settle_video_points_after_success(
    db,
    task_record_id: UUID,
    ai_model: AiModel,
) -> None:
    task_record = await db.get(UserTaskRecord, task_record_id)
    if task_record is None or task_record.status != "success":
        return
    try:
        await settle_video_task_points(
            db,
            task_record,
            ai_model,
            (task_record.extra or {}).get("user_message_extra") or {},
            remark_prefix="对话视频生成",
        )
        await db.commit()
    except Exception:
        await db.rollback()
        task_record = await db.get(UserTaskRecord, task_record_id)
        if task_record is None or task_record.status != "success":
            return
        task_record.extra = {
            **(task_record.extra or {}),
            "points_settlement_failed": "积分结算失败，已保留生成结果",
        }
        await db.commit()


async def _mark_retrying(
    db,
    task_record: UserTaskRecord,
    assistant_message: ConversationMessage,
    reason: str,
) -> None:
    reason = sanitize_public_message(reason, fallback="模型服务繁忙，正在重试")
    task_record.status = "pending"
    task_record.result = reason
    task_record.extra = {
        **(task_record.extra or {}),
        "retry_reason": reason,
    }
    assistant_message.content = "模型服务繁忙，任务正在自动重试"
    assistant_message.extra = {
        **(assistant_message.extra or {}),
        "task_status": "pending",
        "retry_reason": reason,
        "task_record_id": str(task_record.id),
    }
    if assistant_message.message_type == "text":
        assistant_message.status = "pending"
    await db.commit()


async def _resolve_provider_task_result(
    model_snapshot: SimpleNamespace,
    generation_type: str,
    model_result: ModelRunResult,
) -> ModelRunResult:
    task_id = model_result.extra.get("task_id")
    if generation_type not in {"image", "video"} or not task_id:
        return model_result

    if settings.provider_task_worker_poll_max_attempts <= 0:
        model_result.extra = {
            **model_result.extra,
            "platform_task_status": "running",
            "provider_polling_deferred": True,
            "next_poll_seconds": provider_poll_interval_seconds(generation_type),
        }
        model_result.content = f"模型任务仍在生成中：{task_id}"
        return model_result

    latest_result = model_result
    for _ in range(settings.provider_task_worker_poll_max_attempts):
        await asyncio.sleep(settings.provider_task_worker_poll_interval_seconds)
        try:
            latest_result = await query_model_task(model_snapshot, generation_type, str(task_id))
        except Exception as exc:
            if _is_retryable_provider_error(exc):
                continue
            raise
        status = str(latest_result.extra.get("task_status") or "").lower()
        if _is_provider_failed_status(status):
            raise RuntimeError(f"模型任务执行失败：{status}")
        if _is_provider_success_result(latest_result, status):
            latest_result.extra = {**latest_result.extra, "platform_task_status": "success"}
            return latest_result

    latest_result.extra = {
        **latest_result.extra,
        "platform_task_status": "running",
        "provider_polling_timeout": True,
        "next_poll_seconds": provider_poll_interval_seconds(generation_type),
    }
    latest_result.content = f"模型任务仍在生成中：{task_id}"
    return latest_result


def _is_provider_success_result(model_result: ModelRunResult, status: str) -> bool:
    terminal_success_statuses = {
        "success",
        "succeeded", "completed", "complete", "finished",
        "done",
    }
    pending_statuses = {
        "not_start",
        "submitted", "in_progress", "running", "pending", "processing",
        "queued",
    }
    if status in terminal_success_statuses:
        return True
    if status in pending_statuses:
        return False
    return bool(model_result.content and model_result.content != "生成任务处理中")


def _is_provider_failed_status(status: str) -> bool:
    return status in {"failed", "failure", "fail", "error", "canceled", "cancelled"}


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
