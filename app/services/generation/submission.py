"""Stage a charged generation and its delivery intent in the caller's transaction."""

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.task_record import UserTaskRecord
from app.services.billing.points import consume_user_points
from app.services.generation.task_dispatch import enqueue_task_dispatch
from app.services.generation.task_records import create_user_task_record


@asynccontextmanager
async def pending_generation(
    db: AsyncSession,
    *,
    user_id: UUID,
    ai_model_id: UUID,
    business_type: str,
    business_id: UUID,
    generation_type: Literal["text", "image", "video"],
    title: str,
    prompt: str,
    points_cost: int,
    charge_remark: str,
    extra: dict[str, Any],
    task_name: str,
    task_args: tuple[str, ...] = (),
    expire_stale: bool = False,
) -> AsyncIterator[UserTaskRecord]:
    """Write business rows inside this block; commit and publish only after it exits.

    The savepoint rolls back the charge, task, business writes and outbox together.
    Delivery uses the task ID as its message ID and first worker argument. This
    function neither commits the outer transaction nor contacts the broker.
    """
    async with db.begin_nested():
        transaction = (
            await consume_user_points(
                db, user_id, points_cost, remark=charge_remark, auto_commit=False,
            )
            if points_cost > 0
            else None
        )
        task = await create_user_task_record(
            db,
            user_id=user_id,
            ai_model_id=ai_model_id,
            business_type=business_type,
            business_id=business_id,
            generation_type=generation_type,
            status="pending",
            title=title,
            prompt=prompt,
            points_cost=points_cost,
            points_transaction_id=transaction.id if transaction else None,
            extra=extra,
            expire_stale=expire_stale,
        )
        await db.flush()
        yield task
        await enqueue_task_dispatch(
            db,
            task_name=task_name,
            args=(str(task.id), *task_args),
            queue=f"story_ai_{generation_type}",
            message_id=task.id,
        )
        await db.flush()
