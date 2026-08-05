import asyncio
from uuid import UUID

from app.core.config import settings
from app.db.session import create_worker_sessionmaker
from app.services.agent_production_controller import (
    advance_agent_batch_production,
    fail_agent_controller_claim,
    finish_agent_controller_claim,
    list_agent_controller_candidates,
    queue_agent_controller_claim,
    start_agent_controller_claim,
)
from app.worker import celery_app


WorkerSessionLocal = create_worker_sessionmaker()


@celery_app.task(
    name="tasks.agent_production_controller.advance_agent_batch_production",
    soft_time_limit=settings.effective_celery_task_soft_time_limit_seconds,
    time_limit=settings.effective_celery_task_time_limit_seconds,
)
def advance_agent_batch_production_task(production_id: str, lease_token: str) -> bool:
    return asyncio.run(
        _advance_agent_batch_production(UUID(production_id), UUID(lease_token))
    )


@celery_app.task(
    name="tasks.agent_production_controller.enqueue_agent_batch_productions",
    soft_time_limit=settings.effective_celery_task_soft_time_limit_seconds,
    time_limit=settings.effective_celery_task_time_limit_seconds,
)
def enqueue_agent_batch_productions(limit: int = 100) -> int:
    return asyncio.run(_enqueue_agent_batch_productions(limit))


async def _advance_agent_batch_production(production_id: UUID, lease_token: UUID) -> bool:
    async with WorkerSessionLocal() as db:
        if not await start_agent_controller_claim(db, production_id, lease_token):
            return False
        try:
            advanced = await advance_agent_batch_production(db, production_id)
        except Exception as exc:
            await fail_agent_controller_claim(db, production_id, lease_token, str(exc))
            raise
        await finish_agent_controller_claim(
            db,
            production_id,
            lease_token,
            advanced=advanced,
        )
        return advanced


async def _enqueue_agent_batch_productions(limit: int) -> int:
    async with WorkerSessionLocal() as db:
        production_ids = await list_agent_controller_candidates(db, limit=max(1, limit))
        claims = []
        for production_id in production_ids:
            lease_token = await queue_agent_controller_claim(db, production_id)
            if lease_token is not None:
                claims.append((production_id, lease_token))
    for production_id, lease_token in claims:
        advance_agent_batch_production_task.apply_async(
            args=(str(production_id), str(lease_token)),
            queue="story_ai_default",
            routing_key="story_ai_default",
        )
    return len(claims)
