from collections.abc import AsyncGenerator

from typing import Optional

from sqlalchemy import NullPool
from sqlalchemy.ext.asyncio import ( AsyncEngine, AsyncSession, async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=settings.postgres_pool_size,
    max_overflow=settings.postgres_max_overflow,
    pool_timeout=settings.postgres_pool_timeout,
    pool_recycle=settings.postgres_pool_recycle,
    echo=settings.log_sql_enabled,
    connect_args={"server_settings": {"timezone": settings.timezone}},
)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

_worker_engine: Optional[AsyncEngine] = None
_worker_sessionmaker: Optional[async_sessionmaker[AsyncSession]] = None


def create_worker_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _worker_engine, _worker_sessionmaker
    if _worker_sessionmaker is None:
        _worker_engine = create_async_engine(
            settings.database_url,
            poolclass=NullPool,
            echo=settings.log_sql_enabled,
            connect_args={"server_settings": {"timezone": settings.timezone}},
        )
        _worker_sessionmaker = async_sessionmaker(
            _worker_engine, expire_on_commit=False, class_=AsyncSession
        )
    return _worker_sessionmaker


async def dispose_engine() -> None:
    await engine.dispose()


async def dispose_worker_engine() -> None:
    global _worker_engine, _worker_sessionmaker
    if _worker_engine is not None:
        await _worker_engine.dispose()
    _worker_engine = None
    _worker_sessionmaker = None


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session
