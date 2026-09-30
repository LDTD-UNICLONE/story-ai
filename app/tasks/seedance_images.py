"""Short review jobs; PostgreSQL due times and beat recover interrupted workers."""

import asyncio
import logging
from datetime import timedelta
from uuid import UUID

from openai import APIStatusError, APITimeoutError
from sqlalchemy import select

from app.core.celery_app import celery_app
from app.core.config import settings
from app.core.timezone import beijing_datetime
from app.db.session import create_worker_sessionmaker
from app.integrations import apimart
from app.models.seedance_image import SeedanceImage
from app.services.generation.task_dispatch import enqueue_task_dispatch, dispatch_tasks_best_effort
from app.services.seedance_images import REVIEW_TASK, provider_scope


WorkerSessionLocal = create_worker_sessionmaker()
logger = logging.getLogger(__name__)


@celery_app.task(name=REVIEW_TASK)
def review_image(image_id: str) -> None:
    asyncio.run(_review_image(UUID(image_id)))


@celery_app.task(name="tasks.seedance_images.enqueue_due_reviews")
def enqueue_due_reviews(limit: int = 100) -> int:
    return asyncio.run(_enqueue_due_reviews(limit))


async def _enqueue_due_reviews(limit: int) -> int:
    async with WorkerSessionLocal() as db:
        ids = list(
            (
                await db.scalars(
                    select(SeedanceImage.id)
                    .where(
                        SeedanceImage.next_poll_at <= beijing_datetime(),
                        SeedanceImage.status.in_(("pending", "submitting", "processing")),
                    )
                    .order_by(SeedanceImage.next_poll_at, SeedanceImage.id)
                    .limit(max(1, limit))
                )
            ).all()
        )
        for image_id in ids:
            await enqueue_task_dispatch(
                db,
                task_name=REVIEW_TASK,
                args=(str(image_id),),
                queue="story_ai_default",
                message_id=image_id,
            )
        await db.commit()
        await dispatch_tasks_best_effort(db, ids)
        return len(ids)


def usable_asset(data: dict) -> str | None:
    result = data.get("result") or {}
    if not isinstance(result, dict):
        return None
    # We submit exactly one file. Never guess its identity from a multi-file result.
    assets = result.get("assets") or []
    usable = result.get("usable_assets") or assets
    if (
        isinstance(assets, list)
        and len(assets) <= 1
        and isinstance(usable, list)
        and len(usable) == 1
    ):
        item = usable[0]
        if isinstance(item, dict) and item.get("status") == "Active":
            url = item.get("asset_url")
            if isinstance(url, str) and url.startswith("asset://") and 8 < len(url) <= 512:
                return url
    url = result.get("asset_url")
    if (
        not assets
        and not usable
        and data.get("status") == "completed"
        and isinstance(url, str)
        and url.startswith("asset://")
        and 8 < len(url) <= 512
    ):
        return url
    return None


async def _review_image(image_id: UUID) -> None:
    try:
        await _advance_review(image_id)
    finally:
        await apimart.close_apimart_client()


async def _advance_review(image_id: UUID) -> None:
    async with WorkerSessionLocal() as db:
        record = await db.scalar(
            select(SeedanceImage)
            .where(SeedanceImage.id == image_id)
            .with_for_update(skip_locked=True)
        )
        now = beijing_datetime()
        if record is None or record.status not in {"pending", "submitting", "processing"}:
            return
        if record.next_poll_at and record.next_poll_at > now:
            return
        if record.provider_scope != provider_scope():
            record.status, record.error_message = "uncertain", "素材服务配置已变更，请重新上传图片"
            record.next_poll_at = None
            await db.commit()
            return
        if record.status == "submitting":
            record.status, record.error_message = (
                "uncertain",
                "素材提交结果不明确，请联系管理员核对",
            )
            record.next_poll_at = None
            await db.commit()
            return
        submitting = record.status == "pending"
        if submitting:
            record.status = "submitting"
            record.review_started_at = now
        else:
            record.poll_attempts += 1
        # Commit the claim before network I/O. A killed POST is never blindly replayed.
        record.next_poll_at = now + timedelta(
            seconds=max(60, settings.apimart_timeout_seconds + 30)
        )
        source_url = record.upload["url"]
        provider_task_id = record.provider_task_id
        claim_expires_at = record.next_poll_at
        await db.commit()

        data = None
        failure = None
        try:
            data = (
                await apimart.create_private_avatar(source_url, f"image-{image_id}")
                if submitting
                else await apimart.query_generation_task(provider_task_id)
            )
        except Exception as exc:
            failure = exc
            # Record diagnostic identifiers, never request URLs, credentials or bodies.
            logger.warning(
                "Seedance image review request failed: image_id=%s phase=%s "
                "provider_task_id=%s error_type=%s http_status=%s timeout_seconds=%s",
                image_id,
                "submit" if submitting else "query",
                provider_task_id,
                type(exc).__name__,
                getattr(exc, "status_code", None),
                settings.apimart_timeout_seconds,
            )

        await db.refresh(record, with_for_update=True)
        # A manual retry or another lease must not receive this older request's result.
        if record.next_poll_at != claim_expires_at:
            return
        if record.status != ("submitting" if submitting else "processing"):
            return
        if not submitting and record.provider_task_id != provider_task_id:
            return
        now = beijing_datetime()
        if failure is not None:
            if submitting:
                # Explicit HTTP rejection is safe to retry by user action. Timeout,
                # transport loss, 5xx and malformed acknowledgements are ambiguous.
                rejected = isinstance(failure, APIStatusError) and failure.status_code in {
                    400,
                    401,
                    402,
                    403,
                    404,
                    422,
                    429,
                }
                record.status = "failed" if rejected else "uncertain"
                record.error_message = (
                    "素材提交被拒绝，请检查图片或服务配置后重试"
                    if rejected
                    else "素材提交结果不明确，请联系管理员核对"
                )
                if isinstance(failure, (APITimeoutError, TimeoutError)):
                    record.error_message = "素材提交响应超时，无法确认是否已入库，请联系管理员核对"
                record.next_poll_at = None
            else:
                record.error_message = "素材状态暂时无法查询，正在自动重试"
                record.next_poll_at = now + timedelta(
                    seconds=min(60, 5 * 2 ** min(record.poll_attempts, 4))
                )
        elif submitting:
            record.provider_task_id = str(data["id"])
            record.status = "processing"
            record.next_poll_at = now + timedelta(seconds=5)
            record.error_message = None
        else:
            asset = usable_asset(data)
            status = str(data.get("status") or "").lower()
            record.error_message = None
            if asset:
                record.status, record.asset_url, record.next_poll_at = "ready", asset, None
            elif status in {"failed", "canceled", "cancelled"}:
                record.status, record.next_poll_at = "failed", None
                record.error_message = "图片未通过素材审核，请更换图片或重新提交审核"
            elif status == "completed":
                record.status, record.next_poll_at = "uncertain", None
                record.error_message = "审核任务已结束但未返回可用素材，请联系管理员核对"
            else:
                record.next_poll_at = now + timedelta(
                    seconds=min(20, 5 * 2 ** min(record.poll_attempts, 2))
                )
        # A delayed worker must fetch an existing task's result before timing out.
        # The provider may have completed the review while our scheduler was offline.
        if (
            not submitting
            and record.status == "processing"
            and record.review_started_at
            and now - record.review_started_at > timedelta(minutes=30)
        ):
            record.status = "uncertain"
            record.error_message = "素材审核查询超时，请联系管理员核对"
            record.next_poll_at = None
        await db.commit()
