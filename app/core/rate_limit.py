import re
from typing import Optional

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from app.core.config import settings
from app.core.responses import error
from app.integrations.redis import get_redis


class RedisRateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        if not settings.rate_limit_enabled:
            return await call_next(request)

        redis = get_redis()
        if redis is None:
            return await call_next(request)

        rule = _match_rule(request)
        identity = _identity(request, rule)
        window = settings.rate_limit_window_seconds
        keys = _rate_limit_keys(rule)

        for rule_name, limit in keys:
            if limit <= 0:
                continue
            redis_key = f"rate_limit:{rule_name}:{identity}:{window}"
            ttl = window
            try:
                current = await redis.incr(redis_key)
                if current == 1:
                    await redis.expire(redis_key, window)
                else:
                    current_ttl = await redis.ttl(redis_key)
                    if current_ttl > 0:
                        ttl = current_ttl
            except Exception:
                return await call_next(request)
            if current > limit:
                return _rate_limited_response(rule_name, limit, ttl)

        return await call_next(request)


def _match_rule(request: Request) -> str:
    path = request.url.path
    method = request.method.upper()
    if method == "GET" and _is_polling_path(path):
        return "polling"
    if path.endswith("/auth/login") or path.endswith("/auth/register") or path.endswith("/auth/register/sms-code"):
        return "auth"
    if path.endswith("/uploads/file") and method == "POST":
        return "upload"
    if "/conversations/" in path and path.endswith("/messages") and method == "POST":
        return "generation"
    if path.endswith(("/processing", "/analysis", "/image-generation", "/video-generation")) and method == "POST":
        return "generation"
    return "global"


def _is_polling_path(path: str) -> bool:
    if "/task-records/" in path:
        return True
    if "/generation-tasks/" in path:
        return True
    if "/storyboards/" in path and not path.endswith(("/video-generation", "/analyze")):
        return True
    return False


def _rate_limit_keys(rule: str) -> list[tuple[str, int]]:
    if rule == "polling":
        return [(rule, _limit_for_rule(rule))]
    return [
        ("global", settings.rate_limit_global_requests),
        (rule, _limit_for_rule(rule)),
    ]


def _limit_for_rule(rule: str) -> int:
    if rule == "auth":
        return settings.rate_limit_auth_requests
    if rule == "upload":
        return settings.rate_limit_upload_requests
    if rule == "generation":
        return settings.rate_limit_generation_requests
    if rule == "polling":
        return settings.rate_limit_polling_requests
    return 0


def _identity(request: Request, rule: str = "global") -> str:
    user_id: Optional[str] = getattr(request.state, "user_id", None)
    polling_key = _polling_identity_key(request.url.path) if rule == "polling" else None
    if user_id:
        return f"user:{user_id}:{polling_key}" if polling_key else f"user:{user_id}"
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        identity = f"ip:{forwarded_for.split(',')[0].strip()}"
        return f"{identity}:{polling_key}" if polling_key else identity
    host = request.client.host if request.client else "unknown"
    identity = f"ip:{host}"
    return f"{identity}:{polling_key}" if polling_key else identity


def _polling_identity_key(path: str) -> Optional[str]:
    generation_task_match = re.search(r"/generation-tasks/([^/]+)$", path)
    if generation_task_match:
        return f"generation-task:{generation_task_match.group(1)}"
    task_record_match = re.search(r"/task-records/([^/]+)$", path)
    if task_record_match:
        return f"task-record:{task_record_match.group(1)}"
    storyboard_match = re.search(r"/storyboards/([^/]+)$", path)
    if storyboard_match:
        return f"storyboard:{storyboard_match.group(1)}"
    return None


def _rate_limited_response(rule_name: str, limit: int, retry_after_seconds: int) -> Response:
    retry_after_seconds = max(1, retry_after_seconds)
    message = "轮询过于频繁，请稍后再试" if rule_name == "polling" else "请求过于频繁，请稍后再试"
    response = error(
        message=message,
        code=42900,
        data={
            "rule": rule_name,
            "limit": limit,
            "window_seconds": settings.rate_limit_window_seconds,
            "retry_after_seconds": retry_after_seconds,
            "next_poll_seconds": retry_after_seconds,
        },
        http_status=429,
    )
    response.headers["Retry-After"] = str(retry_after_seconds)
    response.headers["X-RateLimit-Limit"] = str(limit)
    response.headers["X-RateLimit-Remaining"] = "0"
    response.headers["X-RateLimit-Reset"] = str(retry_after_seconds)
    return response
