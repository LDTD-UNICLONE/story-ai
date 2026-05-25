from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging
from app.core.rate_limit import RedisRateLimitMiddleware
from app.core.startup import validate_runtime_config
from app.integrations.comfly import close_comfly_client, init_comfly_client
from app.integrations.redis import close_redis, init_redis
from app.integrations.volcengine_ark import close_volcengine_ark_client, init_volcengine_ark_client

configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_runtime_config()
    await init_redis()
    await init_comfly_client()
    if settings.volcengine_ark_api_key:
        await init_volcengine_ark_client()
    yield
    await close_comfly_client()
    await close_volcengine_ark_client()
    await close_redis()


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

    register_exception_handlers(app)
    app.include_router(api_router, prefix=settings.api_prefix)
    return app


app = create_app()
