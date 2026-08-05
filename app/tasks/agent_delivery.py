import asyncio
from uuid import UUID

from app.db.session import create_worker_sessionmaker
from app.services.agent_reviews import (
    claim_agent_delivery,
    fail_agent_delivery,
    run_agent_delivery,
)
from app.worker import celery_app


WorkerSessionLocal = create_worker_sessionmaker()


@celery_app.task(bind=True, name="tasks.agent_delivery.build_agent_delivery", max_retries=3)
def build_agent_delivery(self, delivery_id: str) -> None:
    retry_after = asyncio.run(_build_agent_delivery(UUID(delivery_id)))
    if retry_after:
        raise self.retry(countdown=retry_after)


async def _build_agent_delivery(delivery_id: UUID) -> int:
    async with WorkerSessionLocal() as db:
        lease_token, retry_after = await claim_agent_delivery(db, delivery_id)
        if lease_token is None:
            return retry_after
        try:
            await run_agent_delivery(db, delivery_id, lease_token)
        except Exception as exc:
            await fail_agent_delivery(
                db,
                delivery_id,
                "成片交付生成失败",
                raw_error=str(exc) or exc.__class__.__name__,
                lease_token=lease_token,
            )
        return 0
