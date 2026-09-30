"""Recover accepted provider tasks using committed leases and locked finalization.

Claim transactions commit before provider queries and media persistence. Finalization
commits task state, business results and points together; the caller must roll back
on failure (worker sessions do this on exit).
"""

import asyncio
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4, uuid5

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.public_messages import sanitize_public_message
from app.core.timezone import beijing_datetime, to_beijing_datetime
from app.models.ai_model import AiModel
from app.models.task_record import UserTaskRecord
from app.services.generation.media import persist_generated_media_to_oss
from app.services.billing.model_points import (
    refund_task_points,
    settle_image_task_points,
    settle_video_task_points,
)
from app.services.generation.runner import ModelRunResult, query_model_task
from app.services.generation.provider_polling import provider_poll_interval_seconds
from app.services.generation.provider_state import (
    extract_provider_progress_percent,
    extract_provider_status,
    extract_provider_task_id,
    has_provider_task_id,
    provider_task_id_sql_condition,
    record_provider_task_state,
)
from app.services.generation.business_results import (
    sync_reconciled_task_failure,
    sync_reconciled_task_success,
)
from app.services.generation.task_records import expire_stale_task_record
from app.services.generation.task_dispatch import enqueue_task_dispatch, dispatch_tasks_best_effort
from app.services.generation.provider_budget import provider_query_slot, ProviderQueryDeferred, retry_delay
from app.services.generation.task_events import publish_task_change


@dataclass
class ProviderReconcileClaim:
    task_record_id: UUID
    claim_id: str
    provider_generation_type: str
    provider_task_id: str
    ai_model: AiModel


async def reconcile_provider_task_record(
    db: AsyncSession, task_record_id: UUID
) -> Optional[UserTaskRecord]:
    claim = await _claim_provider_reconcile(db, task_record_id)
    if claim is None:
        return None

    try:
        async with provider_query_slot(claim.ai_model.vendor):
            async with asyncio.timeout(settings.provider_query_timeout_seconds):
                model_result = await query_model_task(
                    claim.ai_model, claim.provider_generation_type, claim.provider_task_id,
                )
    except ProviderQueryDeferred as exc:
        return await _finish_provider_reconcile_claim(
            db, claim, model_result=None, query_failed=True, retry_after=exc.delay,
        )
    except Exception as exc:
        return await _finish_provider_reconcile_claim(
            db, claim, model_result=None, query_failed=True, query_error=exc,
        )
    return await _finish_provider_reconcile_claim(
        db, claim, model_result=model_result, query_failed=False,
    )


async def transfer_provider_task_media(db: AsyncSession, task_record_id: UUID):
    """Resume only the saved media result; never submit or poll a provider here."""
    claim = await _claim_provider_reconcile(db, task_record_id, transfer=True)
    if claim is None:
        return None
    record = await db.get(UserTaskRecord, task_record_id)
    raw_result = dict(record.extra["provider_completed_result"])
    await db.rollback()  # Do not hold a connection/transaction across file transfer.
    try:
        result = await persist_generated_media_to_oss(
            claim.provider_generation_type, ModelRunResult(**raw_result),
        )
    except Exception as exc:
        return await _finish_provider_reconcile_claim(
            db, claim, model_result=None, query_failed=True, query_error=exc, transfer=True,
        )
    return await _finish_provider_reconcile_claim(
        db, claim, model_result=result, query_failed=False, transfer=True,
    )


async def dispatch_provider_media(db, record):
    dispatch_id = await enqueue_task_dispatch(
        db, task_name="tasks.provider_reconcile.transfer_provider_media",
        args=[str(record.id)], queue="story_ai_media",
        message_id=uuid5(record.id, "provider-media-transfer"),
        not_before=record.next_reconcile_at,
    )
    await db.commit()
    await publish_task_change(record)
    await dispatch_tasks_best_effort(db, [dispatch_id])


async def reconcile_provider_task_result(db: AsyncSession, record: UserTaskRecord) -> None:
    await reconcile_provider_task_record(db, record.id)


async def _claim_provider_reconcile(
    db: AsyncSession, task_record_id: UUID, *, transfer: bool = False
) -> Optional[ProviderReconcileClaim]:
    result = await db.execute(
        select(UserTaskRecord)
        .where(UserTaskRecord.id == task_record_id)
        .with_for_update(skip_locked=True)
    )
    record = result.scalar_one_or_none()
    if record is None:
        await db.rollback()
        return None
    if record.status not in {"pending", "running"}:
        await db.rollback()
        return None
    if bool((record.extra or {}).get("provider_completed_result")) != transfer:
        await db.rollback()
        return None
    if await expire_stale_task_record(db, record):
        return None

    provider_generation_type = _provider_generation_type(record)
    is_canvas = bool((record.extra or {}).get("canvas_generation_id"))
    if provider_generation_type is None or (record.ai_model_id is None and not is_canvas):
        await db.rollback()
        return None

    task_id = getattr(record, "provider_task_id", None) or extract_provider_task_id(
        record.extra or {}
    )
    if not task_id or _should_skip_provider_reconcile(record):
        await db.rollback()
        return None
    if _has_active_provider_reconcile_claim(record.extra or {}):
        await db.rollback()
        return None

    if is_canvas:
        from app.services.projects.canvas_results import runtime_model
        from app.services.seedance_images import provider_scope
        from app.core.exceptions import AppException
        scope = record.extra["canvas_snapshot"].get("seedance_provider_scope")
        try:
            scope_matches = not scope or scope == provider_scope()
        except AppException:
            scope_matches = False
        if not scope_matches and not transfer:
            record.extra = {**record.extra, "reconcile_blocked_reason": "素材供应商配置已变更，请恢复原配置后继续查询"}
            await _mark_next_reconcile(db, record, None)
            return None
        ai_model = runtime_model(record)
    else:
        ai_model = await db.get(AiModel, record.ai_model_id)
    if ai_model is None:
        await db.rollback()
        return None

    claim_id = str(uuid4())
    now = beijing_datetime()
    record.provider_task_id = task_id
    record.provider_vendor = record.provider_vendor or ai_model.vendor
    record.provider_status = record.provider_status or "submitted"
    record.provider_submitted_at = record.provider_submitted_at or record.updated_at or now
    if not transfer:
        record.last_reconcile_at = now
    record.next_reconcile_at = now + timedelta(seconds=_provider_reconcile_lease_seconds(transfer=transfer))
    if not transfer:
        record.reconcile_attempts = int(record.reconcile_attempts or 0) + 1
    record.extra = {
        **{key: value for key, value in (record.extra or {}).items() if key != "reconcile_blocked_reason"},
        "provider_reconcile_claim_id": claim_id,
        "provider_reconcile_claimed_at": now.isoformat(),
        "provider_reconcile_claim_until": (
            now + timedelta(seconds=_provider_reconcile_lease_seconds(transfer=transfer))
        ).isoformat(),
    }
    if transfer:
        record.extra = {**record.extra, "media_transfer_started_at": now.isoformat()}
    await db.commit()
    return ProviderReconcileClaim(
        task_record_id=record.id,
        claim_id=claim_id,
        provider_generation_type=provider_generation_type,
        provider_task_id=task_id,
        ai_model=ai_model,
    )


async def _finish_provider_reconcile_claim(
    db: AsyncSession,
    claim: ProviderReconcileClaim,
    *,
    model_result: Optional[ModelRunResult],
    query_failed: bool,
    query_error: Optional[Exception] = None,
    retry_after: Optional[int] = None,
    transfer: bool = False,
) -> Optional[UserTaskRecord]:
    result = await db.execute(
        select(UserTaskRecord)
        .where(UserTaskRecord.id == claim.task_record_id)
        .with_for_update(skip_locked=True)
    )
    record = result.scalar_one_or_none()
    if record is None:
        await db.rollback()
        return None
    if record.status not in {"pending", "running"}:
        await db.rollback()
        await db.refresh(record)
        return record
    if (record.extra or {}).get("provider_reconcile_claim_id") != claim.claim_id:
        await db.rollback()
        return None

    if query_failed or model_result is None:
        counter = "media_transfer_errors" if transfer else "provider_query_errors"
        attempts = int((record.extra or {}).get(counter, 0)) + (retry_after is None)
        record.extra = {**record.extra, counter: attempts}
        delay = retry_after if retry_after is not None else retry_delay(attempts, query_error)
        await _mark_next_reconcile(db, record, None, delay=delay)
        if transfer:
            await dispatch_provider_media(db, record)
        await db.refresh(record)
        return record

    status = str(model_result.extra.get("task_status") or "").lower()
    if transfer:
        await _mark_reconciled_success(db, record, claim.provider_generation_type, model_result)
        await publish_task_change(record)
        return record
    if _is_provider_failed_status(status):
        await _mark_reconciled_failed(
            db, record, f"模型任务执行失败：{status or 'failed'}", model_result.extra
        )
    elif not _is_provider_success_result(model_result, status):
        previous_progress = (record.extra or {}).get("progress_percent")
        now = beijing_datetime()
        record_provider_task_state(
            record,
            {**model_result.extra, "task_id": claim.provider_task_id},
            provider_vendor=claim.ai_model.vendor,
        )
        record.last_reconcile_at = now
        record.extra = _clear_provider_reconcile_claim(
            {
                **(record.extra or {}),
                "provider_query_errors": 0,
                "last_provider_task_status": model_result.extra,
                "provider_reconciled_at": now.isoformat(),
                "next_poll_seconds": provider_poll_interval_seconds(
                    record.generation_type if record is not None else None
                ),
            }
        )
        await db.commit()
        await db.refresh(record)
        if (record.extra or {}).get("progress_percent") == previous_progress:
            return record
    else:
        now = beijing_datetime()
        record.provider_status = "success"
        record.next_reconcile_at = now
        record.extra = _clear_provider_reconcile_claim({
            **record.extra, "generation_phase": "persisting",
            "provider_completed_result": {"content": model_result.content, "extra": model_result.extra},
            "provider_completed_observed_at": now.isoformat(), "provider_query_errors": 0,
        })
        await dispatch_provider_media(db, record)
    await publish_task_change(record)
    return record


def should_reconcile_provider_task(record: UserTaskRecord) -> bool:
    return (
        record.status in {"pending", "running"}
        and _provider_generation_type(record) is not None
        and has_provider_task_id(record)
        and not (record.extra or {}).get("provider_completed_result")
        and not _has_active_provider_reconcile_claim(record.extra or {})
    )


def should_transfer_provider_media(record) -> bool:
    return (
        record.status in {"pending", "running"}
        and bool((record.extra or {}).get("provider_completed_result"))
        and not _has_active_provider_reconcile_claim(record.extra or {})
    )


def provider_reconcile_delay_seconds(record: Optional[UserTaskRecord] = None) -> int:
    if record is None:
        return 0
    due = getattr(record, "next_reconcile_at", None)
    if due is None:
        return 0
    # Countdown is the remaining due time, never a fresh full polling period
    # and never a client-facing extra.next_poll_seconds from an older deployment.
    return max(0, math.ceil((to_beijing_datetime(due) - beijing_datetime()).total_seconds()))


async def list_provider_reconcile_candidates(
    db: AsyncSession, limit: int = 100
) -> List[UserTaskRecord]:
    now = beijing_datetime()
    result = await db.execute(
        select(UserTaskRecord)
        .where(
            UserTaskRecord.status.in_(("pending", "running")),
            UserTaskRecord.generation_type.in_(
                ("image", "video", "asset_image_generate", "storyboard_image", "storyboard_video")
            ),
            UserTaskRecord.provider_task_id.is_not(None),
            or_(
                UserTaskRecord.next_reconcile_at.is_(None),
                UserTaskRecord.next_reconcile_at <= now,
            ),
        )
        .order_by(UserTaskRecord.next_reconcile_at.asc().nullsfirst(), UserTaskRecord.id.asc())
        .limit(limit)
    )
    records = list(result.scalars().all())
    remaining = max(0, limit - len(records))
    if remaining:
        legacy_result = await db.execute(
            select(UserTaskRecord)
            .where(
                UserTaskRecord.status.in_(("pending", "running")),
                UserTaskRecord.generation_type.in_(
                    (
                        "image",
                        "video",
                        "asset_image_generate",
                        "storyboard_image",
                        "storyboard_video",
                    )
                ),
                UserTaskRecord.provider_task_id.is_(None),
                provider_task_id_sql_condition(),
            )
            .order_by(UserTaskRecord.updated_at.asc())
            .limit(remaining)
        )
        records.extend(legacy_result.scalars().all())
    return [record for record in records if should_reconcile_provider_task(record) or should_transfer_provider_media(record)]


def _provider_generation_type(record: UserTaskRecord) -> Optional[str]:
    if (record.extra or {}).get("canvas_generation_id") and record.generation_type in {"image", "video"}:
        return record.generation_type
    if record.business_type == "conversation" and record.generation_type in {"image", "video"}:
        return record.generation_type
    if record.business_type == "project" and record.generation_type == "asset_image_generate":
        return "image"
    if record.business_type == "project" and record.generation_type == "storyboard_image":
        return "image"
    if record.business_type == "project" and record.generation_type == "storyboard_video":
        return "video"
    return None


def _video_request_extra(record: UserTaskRecord) -> Dict[str, Any]:
    extra = record.extra or {}
    for key in ("user_message_extra", "model_extra"):
        value = extra.get(key)
        if isinstance(value, dict):
            return value
    return {}


async def _mark_next_reconcile(
    db: AsyncSession,
    record: UserTaskRecord,
    provider_extra: Optional[Dict[str, Any]],
    *, delay: Optional[int] = None,
) -> None:
    now = beijing_datetime()
    record.last_reconcile_at = now
    record.next_reconcile_at = now + timedelta(
        seconds=delay if delay is not None else provider_poll_interval_seconds()
    )
    if provider_extra is not None:
        provider_status = extract_provider_status(provider_extra)
        if provider_status:
            record.provider_status = provider_status
    extra = _clear_provider_reconcile_claim(
        {
            **(record.extra or {}),
            "provider_reconciled_at": now.isoformat(),
            "next_poll_seconds": provider_poll_interval_seconds(
                record.generation_type if record is not None else None
            ),
        }
    )
    if provider_extra is not None:
        extra["last_provider_task_status"] = provider_extra
    record.extra = extra
    await db.commit()


def _should_skip_provider_reconcile(record: UserTaskRecord) -> bool:
    next_reconcile_at = getattr(record, "next_reconcile_at", None)
    if next_reconcile_at is not None:
        return to_beijing_datetime(next_reconcile_at) > beijing_datetime()
    extra = record.extra or {}
    reconciled_at = extra.get("provider_reconciled_at")
    if not reconciled_at:
        return False
    try:
        last_time = datetime.fromisoformat(str(reconciled_at))
    except ValueError:
        return False
    last_time = to_beijing_datetime(last_time)
    elapsed = (beijing_datetime() - last_time).total_seconds()
    return elapsed < provider_poll_interval_seconds(
        record.generation_type if record is not None else None
    )


def _provider_reconcile_lease_seconds(*, transfer: bool = False) -> int:
    if not transfer:
        return settings.provider_query_timeout_seconds + 30
    return max(60, settings.effective_celery_task_time_limit_seconds + 30)


def _has_active_provider_reconcile_claim(extra: Dict[str, Any]) -> bool:
    claim_id = extra.get("provider_reconcile_claim_id")
    claim_until = extra.get("provider_reconcile_claim_until")
    if not claim_id or not claim_until:
        return False
    try:
        until = datetime.fromisoformat(str(claim_until))
    except ValueError:
        return False
    until = to_beijing_datetime(until)
    return until > beijing_datetime()


def _clear_provider_reconcile_claim(extra: Dict[str, Any]) -> Dict[str, Any]:
    cleaned = dict(extra)
    cleaned.pop("provider_reconcile_claim_id", None)
    cleaned.pop("provider_reconcile_claimed_at", None)
    cleaned.pop("provider_reconcile_claim_until", None)
    return cleaned


async def _mark_reconciled_success(
    db: AsyncSession,
    record: UserTaskRecord,
    provider_generation_type: str,
    model_result: ModelRunResult,
) -> None:
    now = beijing_datetime()
    record.status = "success"
    record.result = model_result.content
    record.provider_status = extract_provider_status(model_result.extra) or "success"
    record.last_reconcile_at = now
    record.next_reconcile_at = None
    record.extra = _clear_provider_reconcile_claim(
        {
            **(record.extra or {}),
            "generation_phase": "completed",
            "media_persisted_at": now.isoformat(),
            "progress_percent": 100,
            "model_result_extra": model_result.extra,
            "provider_reconciled_at": now.isoformat(),
        }
    )

    record.extra = {key: value for key, value in record.extra.items() if key != "provider_completed_result"}
    await sync_reconciled_task_success(db, record, model_result)

    if (record.extra or {}).get("canvas_generation_id"):
        from app.services.projects.canvas_results import settle_completed
        task_id = record.id
        await db.commit()
        await settle_completed(db, task_id)
        await db.refresh(record)
        return

    if provider_generation_type in {"image", "video"} and record.ai_model_id:
        ai_model = await db.get(AiModel, record.ai_model_id)
        if ai_model is not None:
            if provider_generation_type == "image":
                await settle_image_task_points(
                    db,
                    record,
                    ai_model,
                    model_result.extra,
                    remark_prefix="图像生成",
                )
            else:
                await settle_video_task_points(
                    db,
                    record,
                    ai_model,
                    _video_request_extra(record),
                    remark_prefix="视频生成",
                )

    await db.commit()
    await db.refresh(record)


async def _mark_reconciled_failed(
    db: AsyncSession,
    record: UserTaskRecord,
    reason: str,
    provider_extra: Dict[str, Any],
) -> None:
    reason = sanitize_public_message(reason)
    now = beijing_datetime()
    record.status = "failed"
    record.result = reason
    record.provider_status = extract_provider_status(provider_extra) or "failed"
    record.last_reconcile_at = now
    record.next_reconcile_at = None
    reconciled_extra = {
        **(record.extra or {}),
        "failed_reason": reason,
        "model_result_extra": provider_extra,
        "provider_reconciled_at": now.isoformat(),
    }
    progress_percent = extract_provider_progress_percent(provider_extra)
    if progress_percent is not None:
        reconciled_extra["progress_percent"] = progress_percent
    record.extra = _clear_provider_reconcile_claim(reconciled_extra)
    await refund_task_points(db, record, remark_prefix="任务失败退回积分")
    await sync_reconciled_task_failure(db, record, reason)
    await db.commit()
    await db.refresh(record)


def _is_provider_failed_status(status: str) -> bool:
    return status in {"failed", "failure", "fail", "error", "cancelled", "canceled"}


def _is_provider_success_result(model_result: ModelRunResult, status: str) -> bool:
    if _is_provider_failed_status(status):
        return False
    if status in {"success", "succeeded", "completed", "complete", "finished", "done"}:
        return True
    if status in {
        "not_start",
        "submitted",
        "in_progress",
        "running",
        "pending",
        "processing",
        "queued",
    }:
        return False
    content = (model_result.content or "").strip()
    return bool(
        content and content != "生成任务处理中" and not content.startswith("模型任务仍在生成中")
    )
