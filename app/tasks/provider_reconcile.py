import asyncio
from typing import Optional
from uuid import UUID

from app.core.config import settings
from app.db.session import create_worker_sessionmaker
from app.integrations.comfly import close_comfly_client
from app.integrations.volcengine_ark import close_volcengine_ark_client
from app.services.task_records import (
    list_provider_reconcile_candidates,
    provider_reconcile_delay_seconds,
    reconcile_provider_task_record,
    should_reconcile_provider_task,
)
from app.worker import celery_app


WorkerSessionLocal = create_worker_sessionmaker()


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
            raise self.retry(exc=exc, countdown=settings.provider_task_poll_interval_seconds) from exc
        raise


def enqueue_provider_reconcile(task_record_id: str, countdown: Optional[int] = None) -> None:
    reconcile_provider_task.apply_async(
        args=(task_record_id,),
        countdown=provider_reconcile_delay_seconds() if countdown is None else max(0, countdown),
        queue="story_ai_default",
    )


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
            if record is not None and should_reconcile_provider_task(record):
                enqueue_provider_reconcile(
                    str(record.id),
                    countdown=provider_reconcile_delay_seconds(record),
                )
    finally:
        await close_comfly_client()
        await close_volcengine_ark_client()


async def _enqueue_pending_provider_reconciliations(limit: int) -> int:
    async with WorkerSessionLocal() as db:
        records = await list_provider_reconcile_candidates(db, limit=max(1, limit))
        for record in records:
            enqueue_provider_reconcile(
                str(record.id),
                countdown=provider_reconcile_delay_seconds(record),
            )
        return len(records)
