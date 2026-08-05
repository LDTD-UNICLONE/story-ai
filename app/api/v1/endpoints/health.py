from fastapi import APIRouter
from sqlalchemy import text

from app.core.responses import error, success
from app.core.timezone import now_beijing
from app.db.session import AsyncSessionLocal
from app.integrations.redis import get_redis

router = APIRouter()


@router.get("/health")
async def health_check():
    database_ok = False
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
        database_ok = True
    except Exception:
        database_ok = False

    redis = get_redis()
    redis_ok = False
    if redis is not None:
        try:
            redis_ok = await redis.ping()
        except Exception:
            redis_ok = False

    data = {
        "status": "ok" if database_ok and redis_ok else "unavailable",
        "database": database_ok,
        "redis": redis_ok,
        "time": now_beijing(),
    }
    if not database_ok or not redis_ok:
        return error(
            message="服务依赖不可用",
            code=50300,
            data=data,
            http_status=503,
        )
    return success(data=data)
