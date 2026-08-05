from ipaddress import ip_address, ip_network
from typing import Optional
from uuid import UUID

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from app.core.config import settings
from app.core.logging import bind_request_context
from app.core.responses import error
from app.core.security import decode_access_token
from app.integrations.redis import get_redis


class RedisRateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        if not settings.rate_limit_enabled:
            return await call_next(request)

        redis = get_redis()
        if redis is None:
            if _can_bypass_unavailable_rate_limiter():
                return await call_next(request)
            return _rate_limiter_unavailable_response()

        rule = _match_rule(request)
        _bind_user_id_from_bearer_token(request)
        identity = _identity(request)
        window = _window_for_rule(rule)
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
                if _can_bypass_unavailable_rate_limiter():
                    return await call_next(request)
                return _rate_limiter_unavailable_response()
            if current > limit:
                return _rate_limited_response(rule_name, limit, ttl)

        return await call_next(request)


def _match_rule(request: Request) -> str:
    path = request.url.path
    method = request.method.upper()
    if method == "GET" and _is_polling_path(path):
        return "polling"
    if ( path.endswith("/auth/login") or path.endswith("/auth/register") or path.endswith("/auth/register/sms-code")
    ):
        return "auth"
    if method in {"POST", "PUT", "PATCH"} and _is_upload_path(path):
        return "upload"
    if "/conversations/" in path and path.endswith("/messages") and method == "POST":
        return "generation"
    if (
        path.endswith(("/generations", "/image-generations", "/video-generations", "/dispatch"))
        and method == "POST"
    ):
        return "generation"
    if ( path.endswith(
            (
                "/processing", "/analysis", "/image-generation", "/video-generation", "/refine",
                "/storyboard-prompt-generation",
            )
    ) and method == "POST"
    ):
        return "generation"
    return "global"


def _is_upload_path(path: str) -> bool:
    return path.endswith(
        (
            "/uploads/file",
            "/works/uploads",
            "/agent-productions/from-file",
            "/script-supplements/from-file",
            "/source-preview",
        )
    ) or "/admin/materials" in path


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
        return _polling_window_limit()
    return 0


def _window_for_rule(rule: str) -> int:
    if rule == "polling":
        return max(1, settings.rate_limit_polling_window_seconds)
    return max(1, settings.rate_limit_window_seconds)


def _polling_window_limit() -> int:
    base_window = max(1, settings.rate_limit_window_seconds)
    polling_window = _window_for_rule("polling")
    per_window = settings.rate_limit_polling_requests * polling_window // base_window
    return max(5, per_window)


def _identity(request: Request) -> str:
    user_id: Optional[str] = getattr(request.state, "user_id", None)
    if user_id:
        return f"user:{user_id}"
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for and _is_trusted_proxy(request.client.host if request.client else ""):
        forwarded_client = _forwarded_client_ip(forwarded_for)
        if forwarded_client:
            return f"ip:{forwarded_client}"
    host = request.client.host if request.client else "unknown"
    return f"ip:{host}"


def _is_trusted_proxy(host: str) -> bool:
    try:
        address = ip_address(host)
    except ValueError:
        return False
    for value in settings.trusted_proxy_ips:
        try:
            if address in ip_network(value, strict=False):
                return True
        except ValueError:
            continue
    return False


def _forwarded_client_ip(value: str) -> Optional[str]:
    for candidate in reversed(value.split(",")):
        normalized = candidate.strip()
        try:
            ip_address(normalized)
        except ValueError:
            continue
        if not _is_trusted_proxy(normalized):
            return normalized
    return None


def _bind_user_id_from_bearer_token(request: Request) -> None:
    if getattr(request.state, "user_id", None):
        return
    authorization = request.headers.get("authorization") or ""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return
    try:
        subject = decode_access_token(token.strip()).get("sub")
        if subject is None:
            return
        user_id = str(UUID(str(subject)))
    except Exception:
        return
    request.state.user_id = user_id
    bind_request_context(user_id=user_id)


def _can_bypass_unavailable_rate_limiter() -> bool:
    return settings.app_env.lower() in {"local", "development", "dev", "test"}


def _rate_limiter_unavailable_response() -> Response:
    response = error(
        message="请求保护服务暂时不可用，请稍后再试",
        code=50300,
        data={"retry_after_seconds": 1},
        http_status=503,
    )
    response.headers["Retry-After"] = "1"
    return response


def _rate_limited_response(rule_name: str, limit: int, retry_after_seconds: int) -> Response:
    retry_after_seconds = max(1, retry_after_seconds)
    message = "轮询过于频繁，请稍后再试" if rule_name == "polling" else "请求过于频繁，请稍后再试"
    window_seconds = _window_for_rule(rule_name)
    response = error(
        message=message,
        code=42900,
        data={
            "rule": rule_name,
            "limit": limit,
            "window_seconds": window_seconds,
            "retry_after_seconds": retry_after_seconds,
            "next_poll_seconds": retry_after_seconds,
            "hint": "请清理重复轮询定时器，并按 next_poll_seconds 或 Retry-After 后重试" if rule_name == "polling" else None,
        },
        http_status=429,
    )
    response.headers["Retry-After"] = str(retry_after_seconds)
    response.headers["X-RateLimit-Limit"] = str(limit)
    response.headers["X-RateLimit-Remaining"] = "0"
    response.headers["X-RateLimit-Reset"] = str(retry_after_seconds)
    return response
