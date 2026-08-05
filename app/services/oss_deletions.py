import logging
from datetime import datetime, timedelta
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.timezone import beijing_datetime
from app.integrations.oss import OssClient
from app.models.oss_deletion import OssDeletionOutbox


logger = logging.getLogger(__name__)


async def enqueue_oss_deletions(db: AsyncSession, object_keys: Iterable[str]) -> None:
    keys = sorted({key.strip() for key in object_keys if key and key.strip()})
    if not keys:
        return
    statement = (
        insert(OssDeletionOutbox)
        .values([{"object_key": key} for key in keys])
        .on_conflict_do_nothing(index_elements=[OssDeletionOutbox.object_key])
    )
    await db.execute(statement)


async def process_oss_deletion_outbox(
    db: AsyncSession,
    *,
    limit: int = 100,
    object_keys: Optional[Iterable[str]] = None,
) -> int:
    now = beijing_datetime()
    query = (
        select(OssDeletionOutbox)
        .where(OssDeletionOutbox.next_attempt_at <= now)
        .order_by(OssDeletionOutbox.next_attempt_at.asc(), OssDeletionOutbox.created_at.asc())
        .limit(max(1, limit))
        .with_for_update(skip_locked=True)
    )
    if object_keys is not None:
        keys = {key for key in object_keys if key}
        if not keys:
            return 0
        query = query.where(OssDeletionOutbox.object_key.in_(keys))

    result = await db.execute(query)
    records = list(result.scalars().all())
    if not records:
        return 0

    deleted = 0
    try:
        oss_client = OssClient()
    except Exception as exc:
        for record in records:
            _record_failure(record, exc, now)
            _log_deferred_deletion(record)
        await db.commit()
        return 0

    for record in records:
        try:
            await run_in_threadpool(oss_client.delete_object, record.object_key)
        except Exception as exc:
            _record_failure(record, exc, now)
            _log_deferred_deletion(record)
            continue
        await db.delete(record)
        deleted += 1
    await db.commit()
    return deleted


def _record_failure(record: OssDeletionOutbox, exc: Exception, now: datetime) -> None:
    record.attempt_count = int(record.attempt_count or 0) + 1
    delay_seconds = min(3600, 30 * (2 ** min(record.attempt_count - 1, 7)))
    record.next_attempt_at = now + timedelta(seconds=delay_seconds)
    record.last_error = (str(exc) or exc.__class__.__name__)[:1000]


def _log_deferred_deletion(record: OssDeletionOutbox) -> None:
    logger.warning(
        "OSS deletion deferred: object_key=%s attempt=%s next_attempt_at=%s reason=%s",
        record.object_key,
        record.attempt_count,
        record.next_attempt_at.isoformat(),
        record.last_error,
    )
