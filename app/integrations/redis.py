from typing import Optional

from redis.asyncio import Redis

from app.core.config import settings

_redis: Optional[Redis] = None


async def init_redis() -> Redis:
    global _redis
    if _redis is None:
        _redis = Redis.from_url(settings.redis_url(), decode_responses=True)
    return _redis


def get_redis() -> Optional[Redis]:
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None
