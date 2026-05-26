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
from app.models.task_record import UserTaskRecord
from app.services.points import change_user_points
from app.services.project_asset_generation import _asset_image_config, run_asset_image_generation_in_worker
from app.worker import celery_app


WorkerSessionLocal = create_worker_sessionmaker()


@celery_app.task(
    bind=True,
    name="tasks.project_asset_generation.run_project_asset_image_generation",
    max_retries=settings.celery_task_max_retries,
    soft_time_limit=settings.celery_task_soft_time_limit_seconds,
    time_limit=settings.celery_task_time_limit_seconds,
)
def run_project_asset_image_generation(self, task_record_id: str, asset_type: str, asset_id: str) -> None:
    try:
        asyncio.run(_run_project_asset_image_generation(UUID(task_record_id), asset_type, UUID(asset_id)))
    except SoftTimeLimitExceeded:
        asyncio.run(_fail_generation(UUID(task_record_id), asset_type, UUID(asset_id), "任务执行超时"))
    except Exception as exc:
        if self.request.retries < settings.celery_task_max_retries and _is_retryable_provider_error(exc):
            raise self.retry(exc=exc, countdown=_retry_countdown(self.request.retries)) from exc
        asyncio.run(
            _fail_generation(
                UUID(task_record_id),
                asset_type,
                UUID(asset_id),
                _user_failed_reason(exc),
                raw_reason=str(exc) or "任务执行失败",
            )
        )


async def _run_project_asset_image_generation(task_record_id: UUID, asset_type: str, asset_id: UUID) -> None:
    try:
        await _execute_generation(task_record_id, asset_type, asset_id)
    finally:
        await close_comfly_client()
        await close_volcengine_ark_client()


async def _execute_generation(task_record_id: UUID, asset_type: str, asset_id: UUID) -> None:
    config = _asset_image_config(asset_type)
    async with WorkerSessionLocal() as db:
        result = await db.execute(
            select(UserTaskRecord)
            .where(UserTaskRecord.id == task_record_id)
            .with_for_update(skip_locked=True)
        )
        task_record = result.scalar_one_or_none()
        asset = await db.get(config["model"], asset_id)
        if task_record is None or asset is None:
            return
        if task_record.status != "pending":
            return

        task_record.status = "running"
        asset.extra = {
            **(asset.extra or {}),
            "image_generation_status": "running",
            "image_generation_task_record_id": str(task_record.id),
        }
        await db.commit()

        try:
            await run_asset_image_generation_in_worker(db, task_record, asset_type, asset_id)
        except Exception as exc:
            if _is_retryable_provider_error(exc):
                await _mark_retrying(
                    db,
                    task_record,
                    asset,
                    sanitize_public_message(str(exc) or "模型服务繁忙，正在重试"),
                )
                raise
            await _mark_failed(
                db,
                task_record,
                asset,
                sanitize_public_message(str(exc) or "资产图像生成失败"),
                refund=True,
                raw_reason=str(exc) or "资产图像生成失败",
            )
            return
        await db.commit()


async def _fail_generation(
    task_record_id: UUID,
    asset_type: str,
    asset_id: UUID,
    reason: str,
    raw_reason: Optional[str] = None,
) -> None:
    config = _asset_image_config(asset_type)
    async with WorkerSessionLocal() as db:
        task_record = await db.get(UserTaskRecord, task_record_id)
        asset = await db.get(config["model"], asset_id)
        if task_record is None or asset is None:
            return
        if task_record.status in {"success", "failed"}:
            return
        await _mark_failed(db, task_record, asset, reason, refund=True, raw_reason=raw_reason)


async def _mark_failed(
    db,
    task_record: UserTaskRecord,
    asset,
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
    asset.extra = {
        **(asset.extra or {}),
        "image_generation_status": "failed",
        "image_generation_failed_reason": reason,
        "raw_failed_reason": raw_reason or reason,
        "image_generation_task_record_id": str(task_record.id),
    }
    await db.commit()


async def _mark_retrying(db, task_record: UserTaskRecord, asset, reason: str) -> None:
    reason = sanitize_public_message(reason, fallback="模型服务繁忙，正在重试")
    task_record.status = "pending"
    task_record.result = reason
    task_record.extra = {**(task_record.extra or {}), "retry_reason": reason}
    asset.extra = {
        **(asset.extra or {}),
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
