import asyncio
import logging
from datetime import timedelta
from typing import Optional
from uuid import UUID

from app.core.config import settings
from app.db.session import create_worker_sessionmaker
from app.integrations.model_providers import close_model_provider_clients
from app.core.timezone import beijing_datetime
from app.services.apimart_private_avatars import (
    PRIVATE_AVATAR_STAGE,
    mark_private_avatar_resume_scheduled,
    private_avatar_resume_required,
)
from app.services.task_records import (
    list_provider_reconcile_candidates,
    provider_reconcile_delay_seconds,
    reconcile_provider_task_record,
    should_reconcile_provider_task,
)
from app.worker import celery_app


WorkerSessionLocal = create_worker_sessionmaker()
logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    name="tasks.provider_reconcile.reconcile_provider_task",
    max_retries=settings.celery_task_max_retries,
    soft_time_limit=settings.effective_celery_task_soft_time_limit_seconds,
    time_limit=settings.effective_celery_task_time_limit_seconds,
)
def reconcile_provider_task(self, task_record_id: str) -> None:
    try:
        asyncio.run(_reconcile_provider_task(UUID(task_record_id)))
    except Exception as exc:
        if self.request.retries < settings.celery_task_max_retries:
            raise self.retry(
                exc=exc, countdown=settings.provider_task_poll_interval_seconds
            ) from exc
        raise


def enqueue_provider_reconcile(task_record_id: str, countdown: Optional[int] = None) -> None:
    reconcile_provider_task.apply_async(
        args=(task_record_id,),
        countdown=provider_reconcile_delay_seconds() if countdown is None else max(0, countdown),
        queue="story_ai_default",
        routing_key="story_ai_default",
    )


def enqueue_provider_reconcile_best_effort(record) -> bool:
    if not should_reconcile_provider_task(record):
        return False
    try:
        enqueue_provider_reconcile(
            str(record.id),
            countdown=provider_reconcile_delay_seconds(record),
        )
    except Exception:
        logger.exception(
            "Failed to enqueue provider reconciliation; safety sweep will recover it: task_record_id=%s",
            record.id,
        )
        return False
    return True


@celery_app.task(
    name="tasks.provider_reconcile.enqueue_pending_provider_reconciliations",
    soft_time_limit=settings.effective_celery_task_soft_time_limit_seconds,
    time_limit=settings.effective_celery_task_time_limit_seconds,
)
def enqueue_pending_provider_reconciliations(limit: int = 100) -> int:
    return asyncio.run(_enqueue_pending_provider_reconciliations(limit))


async def _reconcile_provider_task(task_record_id: UUID) -> None:
    try:
        async with WorkerSessionLocal() as db:
            record = await reconcile_provider_task_record(db, task_record_id)
            if record is not None and private_avatar_resume_required(record):
                try:
                    _enqueue_generation_after_private_avatar(record)
                except Exception:
                    private_avatar = (record.extra or {}).get("private_avatar") or {}
                    record.status = "running"
                    record.provider_task_id = str(private_avatar.get("task_id") or "") or None
                    record.provider_status = "completed"
                    record.next_reconcile_at = beijing_datetime() + timedelta(
                        seconds=provider_reconcile_delay_seconds(record)
                    )
                    record.extra = {
                        **(record.extra or {}),
                        "provider_stage": PRIVATE_AVATAR_STAGE,
                    }
                    await db.commit()
                    raise
                mark_private_avatar_resume_scheduled(record)
                await db.commit()
                return
            if record is not None and should_reconcile_provider_task(record):
                enqueue_provider_reconcile(
                    str(record.id),
                    countdown=provider_reconcile_delay_seconds(record),
                )
    finally:
        await close_model_provider_clients()


def _enqueue_generation_after_private_avatar(record) -> None:
    extra = record.extra or {}
    if record.business_type == "conversation" and record.generation_type == "video":
        assistant_message_id = str(extra.get("assistant_message_id") or "")
        if not assistant_message_id:
            raise ValueError("对话视频任务缺少 assistant_message_id")
        from app.tasks.model_generation import run_conversation_generation

        run_conversation_generation.apply_async(
            args=(str(record.id), assistant_message_id),
            queue="story_ai_video",
            routing_key="story_ai_video",
        )
        return
    if record.business_type == "project" and record.generation_type == "storyboard_video":
        storyboard_id = str(extra.get("storyboard_id") or "")
        if not storyboard_id:
            raise ValueError("分镜视频任务缺少 storyboard_id")
        from app.tasks.project_storyboard_video import run_project_storyboard_video_generation

        run_project_storyboard_video_generation.apply_async(
            args=(str(record.id), storyboard_id),
            queue="story_ai_video",
            routing_key="story_ai_video",
        )
        return
    raise ValueError("当前任务不支持在人物素材审核后恢复")


async def _enqueue_pending_provider_reconciliations(limit: int) -> int:
    async with WorkerSessionLocal() as db:
        records = await list_provider_reconcile_candidates(db, limit=max(1, limit))
        for record in records:
            enqueue_provider_reconcile(
                str(record.id),
                countdown=0,
            )
        return len(records)
