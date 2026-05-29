import logging
import time
import uuid
from typing import Iterable, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import bind_request_context, clear_request_context, log_extra


REQUEST_ID_HEADER = "X-Request-ID"
access_logger = logging.getLogger("story_ai.access")


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = _request_id(request.headers.get(REQUEST_ID_HEADER))
        bind_request_context(request_id=request_id)
        request.state.request_id = request_id
        start = time.perf_counter()
        response: Optional[Response] = None
        try:
            response = await call_next(request)
            return response
        finally:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            status_code = response.status_code if response is not None else 500
            if response is not None:
                response.headers[REQUEST_ID_HEADER] = request_id
            user_id = getattr(request.state, "user_id", None)
            if user_id:
                bind_request_context(request_id=request_id, user_id=user_id)
            _log_request(request, status_code, duration_ms, request_id)
            clear_request_context()


def _log_request(request: Request, status_code: int, duration_ms: float, request_id: str) -> None:
    level = logging.WARNING if status_code >= 400 else logging.INFO
    client_host = request.client.host if request.client else ""
    access_logger.log(
        level,
        "%s %s %s %s %.2fms",
        request.method,
        request.url.path,
        status_code,
        client_host,
        duration_ms,
        extra=log_extra(
            event="http_request",
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            query=_safe_query(request.url.query),
            status_code=status_code,
            duration_ms=duration_ms,
            client_ip=client_host,
            user_agent=request.headers.get("user-agent", ""),
            referer=request.headers.get("referer", ""),
        ),
    )


def _request_id(raw_value: Optional[str]) -> str:
    value = (raw_value or "").strip()
    if value and _is_safe_header_value(value):
        return value[:128]
    return uuid.uuid4().hex


def _is_safe_header_value(value: str) -> bool:
    return all(char.isalnum() or char in {"-", "_", "."} for char in value)


def _safe_query(query: str) -> str:
    if not query:
        return ""
    hidden_names = ("token", "password", "secret", "key", "authorization")
    parts: Iterable[str] = query.split("&")
    sanitized = []
    for part in parts:
        name = part.split("=", 1)[0].lower()
        if any(hidden in name for hidden in hidden_names):
            sanitized.append(f"{part.split('=', 1)[0]}=***")
        else:
            sanitized.append(part)
    return "&".join(sanitized)[:1000]
