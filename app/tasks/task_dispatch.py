import asyncio

from app.db.session import create_worker_sessionmaker
from app.services.generation.task_dispatch import dispatch_pending_tasks
from app.core.celery_app import celery_app


WorkerSessionLocal = create_worker_sessionmaker()


@celery_app.task(name="tasks.task_dispatch.dispatch_pending_tasks")
def dispatch_pending_tasks_task(limit: int = 100) -> int:
    return asyncio.run(_dispatch_pending_tasks(limit))


async def _dispatch_pending_tasks(limit: int) -> int:
    async with WorkerSessionLocal() as db:
        return await dispatch_pending_tasks(db, limit=max(1, limit))
