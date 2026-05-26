import asyncio
from types import SimpleNamespace
from typing import Optional
from uuid import UUID

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select

from app.core.config import settings
from app.core.exceptions import AppException
from app.core.public_messages import sanitize_public_message
from app.core.timezone import beijing_datetime
from app.db.session import create_worker_sessionmaker
from app.integrations.comfly import close_comfly_client
from app.integrations.volcengine_ark import close_volcengine_ark_client
from app.models.ai_model import AiModel
from app.models.conversation import Conversation, ConversationMessage
from app.models.task_record import UserTaskRecord
from app.services.generated_media import persist_generated_media_to_oss
from app.services.model_points import settle_text_task_points
from app.services.model_runner import ModelRunResult, query_model_task, run_model
from app.services.points import change_user_points
from app.services.task_records import refresh_task_record_interrupted
from app.worker import celery_app


WorkerSessionLocal = create_worker_sessionmaker()


@celery_app.task(
    bind=True,
    name="tasks.model_generation.run_conversation_generation",
    max_retries=settings.celery_task_max_retries,
    soft_time_limit=settings.celery_task_soft_time_limit_seconds,
    time_limit=settings.celery_task_time_limit_seconds,
)
def run_conversation_generation(self, task_record_id: str, assistant_message_id: str) -> None:
    try:
        asyncio.run(_run_conversation_generation(UUID(task_record_id), UUID(assistant_message_id)))
    except SoftTimeLimitExceeded:
        asyncio.run(_fail_generation(UUID(task_record_id), UUID(assistant_message_id), "任务执行超时"))
    except Exception as exc:
        if self.request.retries < settings.celery_task_max_retries:
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
        await _mark_failed(db, task_record, assistant_message, reason, refund=True, raw_reason=raw_reason)


async def _execute_generation(task_record_id: UUID, assistant_message_id: UUID) -> None:
    async with WorkerSessionLocal() as db:
        result = await db.execute(
            select(UserTaskRecord)
            .where(UserTaskRecord.id == task_record_id)
            .with_for_update(skip_locked=True)
        )
        task_record = result.scalar_one_or_none()
        assistant_message = await db.get(ConversationMessage, assistant_message_id)
        if task_record is None or assistant_message is None:
            return
        if task_record.status != "pending":
            return

        task_record.status = "running"
        assistant_message.extra = {**(assistant_message.extra or {}), "task_status": "running"}
        await db.commit()

        result = await db.execute(
            select(AiModel).where(
                AiModel.id == task_record.ai_model_id,
                AiModel.is_enabled.is_(True),
            )
        )
        ai_model = result.scalar_one_or_none()
        if ai_model is None:
            await _mark_failed(db, task_record, assistant_message, "模型不存在或已禁用", refund=True)
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
            )
            model_result = await _resolve_provider_task_result(
                model_snapshot,
                task_record.generation_type,
                model_result,
            )
            model_result = await persist_generated_media_to_oss(task_record.generation_type, model_result)
        except Exception as exc:
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
            )
            return

        if await refresh_task_record_interrupted(db, task_record):
            return

        assistant_message.content = model_result.content
        assistant_message.extra = {
            **(assistant_message.extra or {}),
            **model_result.extra,
            "task_status": "success",
            "task_record_id": str(task_record.id),
        }
        task_record.status = model_result.extra.get("platform_task_status") or "success"
        task_record.result = model_result.content
        task_record.extra = {
            **(task_record.extra or {}),
            "assistant_message_extra": model_result.extra,
            "assistant_message_id": str(assistant_message.id),
        }
        if task_record.business_id:
            await db.execute(
                Conversation.__table__.update()
                .where(Conversation.id == task_record.business_id)
                .values(updated_at=beijing_datetime())
            )
        await db.commit()

        if task_record.generation_type == "text":
            await _settle_text_points_after_success(db, task_record.id, ai_model, model_result.extra)


async def _mark_failed(
    db,
    task_record: UserTaskRecord,
    assistant_message: ConversationMessage,
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
    assistant_message.content = f"任务执行失败：{reason}"
    assistant_message.extra = {
        **(assistant_message.extra or {}),
        "task_status": "failed",
        "failed_reason": reason,
        "raw_failed_reason": raw_reason or reason,
        "task_record_id": str(task_record.id),
    }
    await db.commit()


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
    await db.commit()


async def _resolve_provider_task_result(
    model_snapshot: SimpleNamespace,
    generation_type: str,
    model_result: ModelRunResult,
) -> ModelRunResult:
    task_id = model_result.extra.get("task_id")
    if generation_type not in {"image", "video"} or not task_id:
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
        "next_poll_seconds": settings.provider_task_poll_interval_seconds,
    }
    latest_result.content = f"模型任务仍在生成中：{task_id}"
    return latest_result


def _is_provider_success_result(model_result: ModelRunResult, status: str) -> bool:
    terminal_success_statuses = {"success", "succeeded", "completed", "complete", "finished", "done"}
    if status in terminal_success_statuses:
        return True
    return bool(model_result.content and model_result.content != "生成任务处理中")


def _is_provider_failed_status(status: str) -> bool:
    return status in {"failed", "failure", "fail", "error", "canceled", "cancelled"}


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
