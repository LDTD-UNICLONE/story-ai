from fastapi import APIRouter

from app.core.responses import success
from app.core.timezone import now_beijing
from app.integrations.redis import get_redis

router = APIRouter()


@router.get("/health")
async def health_check():
    redis = get_redis()
    redis_ok = False
    if redis is not None:
        try:
            redis_ok = await redis.ping()
        except Exception:
            redis_ok = False

    return success(
        data={
            "status": "ok",
            "redis": redis_ok,
            "time": now_beijing(),
        }
    )
