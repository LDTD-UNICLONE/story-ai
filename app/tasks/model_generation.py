from app.services.generation.task_events import publish_task_change
import asyncio
import logging
import time
from typing import Optional
from uuid import UUID

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select

from app.core.config import settings
from app.core.logging import log_extra
from app.core.public_messages import sanitize_public_message
from app.core.timezone import beijing_datetime
from app.db.session import create_worker_sessionmaker
from app.integrations.model_providers import close_model_provider_clients
from app.integrations.apimart import APIMART_VENDOR, TextDeltaCallback
from app.models.ai_model import AiModel
from app.models.conversation import Conversation, ConversationMessage
from app.models.task_record import UserTaskRecord
from app.services.generation.media import persist_generated_media_to_oss
from app.services.billing.model_points import (
    refund_task_points,
    settle_image_task_points,
    settle_text_task_points,
    settle_video_task_points,
)
from app.services.models.configuration import build_model_runtime_snapshot
from app.services.generation.runner import run_model
from app.services.generation.provider_polling import defer_provider_task_result
from app.services.generation.provider_state import (
    has_provider_task_id,
    extract_provider_task_id,
    record_provider_task_state,
)
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
logger = logging.getLogger(__name__)
APIMART_STREAM_FLUSH_CHARACTERS = 128
APIMART_STREAM_FLUSH_SECONDS = 0.5


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
        if self.request.retries < settings.celery_task_max_retries and is_retryable_provider_error(
            exc
        ):
            raise self.retry(exc=exc, countdown=retry_countdown(self.request.retries)) from exc
        asyncio.run(
            _fail_generation(
                UUID(task_record_id),
                UUID(assistant_message_id),
                user_failed_reason(exc),
                raw_reason=str(exc) or "任务执行失败",
            )
        )


@celery_app.task(
    name="tasks.model_generation.settle_pending_conversation_points",
    soft_time_limit=settings.effective_celery_task_soft_time_limit_seconds,
    time_limit=settings.effective_celery_task_time_limit_seconds,
)
def settle_pending_conversation_points(limit: int = 100) -> int:
    return asyncio.run(_settle_pending_conversation_points(max(1, limit)))


async def _run_conversation_generation(task_record_id: UUID, assistant_message_id: UUID) -> None:
    try:
        await _execute_generation(task_record_id, assistant_message_id)
    finally:
        await close_model_provider_clients()


async def _settle_pending_conversation_points(limit: int) -> int:
    async with WorkerSessionLocal() as db:
        result = await db.execute(
            select(UserTaskRecord.id)
            .where(
                UserTaskRecord.business_type == "conversation",
                UserTaskRecord.status == "success",
                UserTaskRecord.ai_model_id.is_not(None),
                UserTaskRecord.extra["points_settled"].as_boolean().is_(False),
            )
            .order_by(UserTaskRecord.updated_at.asc(), UserTaskRecord.id.asc())
            .limit(limit)
        )
        task_record_ids = list(result.scalars().all())

    settled_count = 0
    for task_record_id in task_record_ids:
        async with WorkerSessionLocal() as db:
            try:
                result = await db.execute(
                    select(UserTaskRecord)
                    .where(UserTaskRecord.id == task_record_id)
                    .with_for_update(skip_locked=True)
                )
                task_record = result.scalar_one_or_none()
                if (
                    task_record is None
                    or task_record.status != "success"
                    or (task_record.extra or {}).get("points_settled") is not False
                ):
                    await db.rollback()
                    continue
                ai_model = await db.get(AiModel, task_record.ai_model_id)
                if ai_model is None:
                    await db.rollback()
                    continue
                await _settle_completed_conversation_task(db, task_record, ai_model)
                await db.commit()
                await publish_task_change(task_record)
                settled_count += 1
            except Exception:
                await db.rollback()
                logger.exception(
                    "Conversation points settlement recovery failed: task_record_id=%s",
                    task_record_id,
                )
    return settled_count


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
        if task_record.status == "success":
            if not (task_record.extra or {}).get("points_settled"):
                ai_model = await db.get(AiModel, task_record.ai_model_id)
                if ai_model is not None:
                    await _settle_completed_conversation_task(
                        db,
                        task_record,
                        ai_model,
                    )
                    await db.commit()
                    await publish_task_change(task_record)
            return
        if not prepare_task_execution(task_record):
            _enqueue_provider_reconcile_if_needed(task_record)
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
        await publish_task_change(task_record)
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

        model_snapshot = build_model_runtime_snapshot(ai_model)

        try:
            run_kwargs = {"idempotency_key": str(task_record.id)}
            if (
                ai_model.vendor == APIMART_VENDOR
                and task_record.generation_type == "text"
            ):
                run_kwargs["text_stream"] = True
                run_kwargs["on_text_delta"] = _build_apimart_text_delta_callback(
                    db,
                    assistant_message,
                    task_record,
                )
            generation_extra = (task_record.extra or {}).get("user_message_extra") or {}
            model_result = await run_model(
                model_snapshot,
                task_record.generation_type,
                task_record.prompt,
                generation_extra,
                **run_kwargs,
            )
            provider_task_id = extract_provider_task_id(model_result.extra)
            if provider_task_id:
                if not await lock_active_task(db, task_record):
                    return
                record_provider_task_state(task_record, model_result.extra, ai_model.vendor)
                task_record.extra = {
                    **(task_record.extra or {}),
                    **(
                        {"provider_stage": "video_generation"}
                        if task_record.generation_type == "video"
                        else {}
                    ),
                    "assistant_message_extra": model_result.extra,
                    "assistant_message_id": str(assistant_message.id),
                }
                if task_record.generation_type in {"image", "video"}:
                    # Commit provider ownership before handing off; no worker-side polling.
                    model_result = defer_provider_task_result(model_result, provider_task_id)
                    deferred_extra = model_result.extra
                    task_record.result = model_result.content
                    task_record.extra = {
                        **task_record.extra,
                        "assistant_message_extra": deferred_extra,
                    }
                    assistant_message.content = task_record.result
                    assistant_message.extra = {
                        **(assistant_message.extra or {}),
                        **deferred_extra,
                        "task_status": "running",
                        "task_record_id": str(task_record.id),
                    }
                    await db.commit()
                    await publish_task_change(task_record)
                    _enqueue_provider_reconcile_if_needed(task_record)
                    return
                await db.commit()
                await publish_task_change(task_record)
            model_result = await persist_generated_media_to_oss(
                task_record.generation_type, model_result
            )
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

        if not await lock_active_task(db, task_record):
            return
        await db.refresh(assistant_message)

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
        await publish_task_change(task_record)
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

        await _settle_points_after_success(db, task_record.id, ai_model)
        _enqueue_provider_reconcile_if_needed(task_record)


def _build_apimart_text_delta_callback(
    db,
    assistant_message: ConversationMessage,
    task_record: UserTaskRecord,
) -> TextDeltaCallback:
    content_parts: list[str] = []
    pending_characters = 0
    last_flush_at = time.monotonic()
    stopped = False

    async def persist_delta(delta: str) -> None:
        nonlocal pending_characters, last_flush_at, stopped
        if stopped:
            return
        content_parts.append(delta)
        pending_characters += len(delta)
        now = time.monotonic()
        if (
            pending_characters < APIMART_STREAM_FLUSH_CHARACTERS
            and now - last_flush_at < APIMART_STREAM_FLUSH_SECONDS
        ):
            return

        if not await lock_active_task(db, task_record):
            stopped = True
            await db.commit()
            await publish_task_change(task_record)
            return
        await db.refresh(assistant_message)
        content = "".join(content_parts)
        assistant_message.content = content
        assistant_message.extra = {
            **(assistant_message.extra or {}),
            "task_status": "running",
            "stream_started": True,
            "streamed_character_count": len(content),
        }
        await db.commit()
        await publish_task_change(task_record)
        pending_characters = 0
        last_flush_at = now

    return persist_delta


async def _mark_failed(
    db,
    task_record: UserTaskRecord,
    assistant_message: ConversationMessage,
    reason: str,
    refund: bool = False,
    raw_reason: Optional[str] = None,
    failure_extra: Optional[dict] = None,
) -> None:
    if not await lock_active_task(db, task_record, allow_provider_task=False):
        return
    await db.refresh(assistant_message)
    reason = sanitize_public_message(reason)
    if refund:
        await refund_task_points(db, task_record, remark_prefix="任务失败退回积分")
    refund_transaction_id = (task_record.extra or {}).get("refund_transaction_id")

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
    await publish_task_change(task_record)
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


async def _settle_points_after_success(
    db,
    task_record_id: UUID,
    ai_model: AiModel,
) -> None:
    result = await db.execute(
        select(UserTaskRecord)
        .where(UserTaskRecord.id == task_record_id)
        .with_for_update()
    )
    task_record = result.scalar_one_or_none()
    if task_record is None or task_record.status != "success":
        return
    try:
        await _settle_completed_conversation_task(db, task_record, ai_model)
        await db.commit()
        await publish_task_change(task_record)
    except Exception:
        await db.rollback()
        result = await db.execute(
            select(UserTaskRecord)
            .where(UserTaskRecord.id == task_record_id)
            .with_for_update()
        )
        task_record = result.scalar_one_or_none()
        if task_record is None or task_record.status != "success":
            return
        task_record.extra = {
            **(task_record.extra or {}),
            "points_settlement_failed": "积分结算失败，已保留生成结果",
            "points_settled": False,
        }
        await db.commit()
        await publish_task_change(task_record)


async def _settle_completed_conversation_task(
    db,
    task_record: UserTaskRecord,
    ai_model: AiModel,
) -> None:
    if (task_record.extra or {}).get("points_settled"):
        return
    result_extra = _conversation_model_result_extra(task_record)
    if task_record.generation_type == "text":
        await settle_text_task_points(
            db,
            task_record,
            ai_model,
            result_extra,
            remark_prefix="对话模型调用",
        )
    elif task_record.generation_type == "image":
        await settle_image_task_points(
            db,
            task_record,
            ai_model,
            result_extra,
            remark_prefix="对话图像生成",
        )
    elif task_record.generation_type == "video":
        await settle_video_task_points(
            db,
            task_record,
            ai_model,
            (task_record.extra or {}).get("user_message_extra") or {},
            remark_prefix="对话视频生成",
        )


def _conversation_model_result_extra(task_record: UserTaskRecord) -> dict:
    extra = task_record.extra or {}
    for key in ("assistant_message_extra", "model_result_extra"):
        value = extra.get(key)
        if isinstance(value, dict):
            return value
    return {}


async def _mark_retrying(
    db,
    task_record: UserTaskRecord,
    assistant_message: ConversationMessage,
    reason: str,
) -> None:
    if not await lock_active_task(db, task_record, allow_provider_task=False):
        return
    await db.refresh(assistant_message)
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
    await publish_task_change(task_record)


def _enqueue_provider_reconcile_if_needed(task_record: UserTaskRecord) -> None:
    from app.tasks.provider_reconcile import enqueue_provider_reconcile_best_effort

    enqueue_provider_reconcile_best_effort(task_record)
