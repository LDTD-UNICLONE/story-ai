import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging
from app.core.rate_limit import RedisRateLimitMiddleware
from app.core.request_logging import RequestLoggingMiddleware
from app.core.startup import validate_runtime_config
from app.db.session import engine
from app.integrations.model_providers import (
    close_model_provider_clients,
    init_model_provider_clients,
)
from app.integrations.redis import close_redis, init_redis
from app.services.oss_deletions import process_oss_deletion_outbox
from app.services.billing.recharges import purge_expired_pending_recharge_orders
from app.services.works import purge_unused_work_uploads

configure_logging()
logger = logging.getLogger(__name__)
MAINTENANCE_ADVISORY_LOCK_ID = 9_178_240_601


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_runtime_config()
    await init_redis()
    await init_model_provider_clients()
    maintenance_cleanup_task = asyncio.create_task(_maintenance_cleanup_loop())
    yield
    maintenance_cleanup_task.cancel()
    with suppress(asyncio.CancelledError):
        await maintenance_cleanup_task
    await close_model_provider_clients()
    await close_redis()


async def _maintenance_cleanup_loop() -> None:
    while True:
        try:
            await _run_maintenance_cleanup_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Failed to run periodic maintenance cleanup")
        await asyncio.sleep(60)


async def _run_maintenance_cleanup_once() -> bool:
    async with engine.connect() as connection:
        lock_result = await connection.execute(
            text("SELECT pg_try_advisory_lock(CAST(:lock_id AS bigint))"),
            {"lock_id": MAINTENANCE_ADVISORY_LOCK_ID},
        )
        acquired = bool(lock_result.scalar_one())
        await connection.commit()
        if not acquired:
            return False

        try:
            async with AsyncSession(bind=connection, expire_on_commit=False) as db:
                await purge_expired_pending_recharge_orders(db, limit=200)
                await purge_unused_work_uploads(db, limit=100)
                await process_oss_deletion_outbox(db, limit=100)
            return True
        finally:
            await connection.execute(
                text("SELECT pg_advisory_unlock(CAST(:lock_id AS bigint))"),
                {"lock_id": MAINTENANCE_ADVISORY_LOCK_ID},
            )
            await connection.commit()


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        debug=settings.app_debug,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RedisRateLimitMiddleware)
    app.add_middleware(RequestLoggingMiddleware)

    register_exception_handlers(app)
    app.include_router(api_router, prefix=settings.api_prefix)
    frontend_dir = Path(__file__).resolve().parent / "frontend"
    if frontend_dir.exists():
        app.mount("/ui", StaticFiles(directory=frontend_dir, html=True), name="ui")
    return app


app = create_app()
