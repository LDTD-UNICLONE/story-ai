import logging
from datetime import datetime, timedelta
from typing import Optional, Sequence
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.timezone import beijing_datetime
from app.integrations.task_queue import publish_task_message
from app.models.task_dispatch import TaskDispatchOutbox


logger = logging.getLogger(__name__)


async def enqueue_task_dispatch(
    db: AsyncSession,
    *,
    task_name: str,
    args: Sequence[str],
    queue: str,
    message_id: Optional[UUID] = None,
    not_before: Optional[datetime] = None,
) -> UUID:
    """Persist delivery intent inside the business transaction; never publish or commit."""
    message_id = message_id or uuid4()
    await db.execute(
        insert(TaskDispatchOutbox)
        .values(id=message_id, task_name=task_name, args=list(args), queue=queue,
                next_attempt_at=not_before or beijing_datetime())
        .on_conflict_do_nothing(index_elements=[TaskDispatchOutbox.id])
    )
    return message_id


async def dispatch_pending_tasks(
    db: AsyncSession,
    *,
    limit: int = 100,
    message_ids: Optional[Sequence[UUID]] = None,
) -> int:
    """Publish committed intents under outbox row locks, with at-least-once delivery."""
    query = (
        select(TaskDispatchOutbox)
        .where(TaskDispatchOutbox.next_attempt_at <= beijing_datetime())
        .order_by(
            TaskDispatchOutbox.next_attempt_at, TaskDispatchOutbox.created_at, TaskDispatchOutbox.id
        )
        .limit(max(1, limit))
        .with_for_update(skip_locked=True)
    )
    if message_ids is not None:
        query = query.where(TaskDispatchOutbox.id.in_(message_ids))
    records = list((await db.scalars(query)).all())
    published = 0
    for record in records:
        try:
            await run_in_threadpool(
                publish_task_message,
                task_name=record.task_name,
                args=record.args,
                queue=record.queue,
                message_id=str(record.id),
            )
        except Exception as exc:
            record.attempt_count += 1
            record.next_attempt_at = beijing_datetime() + timedelta(
                seconds=min(300, 10 * 2 ** min(record.attempt_count - 1, 5))
            )
            record.last_error = (str(exc) or exc.__class__.__name__)[:1000]
            logger.warning(
                "Task delivery deferred: message_id=%s task=%s attempt=%s",
                record.id,
                record.task_name,
                record.attempt_count,
            )
            # A broker outage should not block this process once per queued message.
            break
        await db.delete(record)
        published += 1
    await db.commit()
    return published


async def dispatch_tasks_best_effort(db: AsyncSession, message_ids: Sequence[UUID]) -> None:
    """Try after the caller's commit, without expiring its response objects on failure."""
    if not message_ids:
        return
    async with AsyncSession(bind=db.bind, expire_on_commit=False) as dispatch_db:
        try:
            await dispatch_pending_tasks(dispatch_db, message_ids=message_ids)
        except Exception:
            await dispatch_db.rollback()
            logger.exception("Task delivery will be retried by the outbox sweep")
