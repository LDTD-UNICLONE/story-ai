import json
import logging
import sys
import traceback
from contextvars import ContextVar
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Optional

from app.core.config import settings


request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
user_id_var: ContextVar[str] = ContextVar("user_id", default="-")


SENSITIVE_KEYS = {
    "authorization",
    "access_token",
    "token",
    "password",
    "password_hash",
    "secret",
    "api_key",
    "key",
    "jwt",
    "cookie",
    "set-cookie",
}


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        record.user_id = user_id_var.get()
        record.app_name = settings.app_name
        record.app_env = settings.app_env
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
            "user_id": getattr(record, "user_id", "-"),
            "app_name": getattr(record, "app_name", settings.app_name),
            "app_env": getattr(record, "app_env", settings.app_env),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
            "process": record.process,
            "thread": record.threadName,
        }
        extra = getattr(record, "extra", None)
        if isinstance(extra, dict):
            payload.update(_sanitize_log_value(extra))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class HumanFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        record.request_id = getattr(record, "request_id", "-")
        record.user_id = getattr(record, "user_id", "-")
        return super().format(record)


def configure_logging() -> None:
    log_dir = Path(settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    context_filter = ContextFilter()
    json_formatter = JsonFormatter()
    console_formatter = HumanFormatter(
        "%(asctime)s %(levelname)s [%(name)s] request_id=%(request_id)s user_id=%(user_id)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    app_handler = _rotating_file_handler(log_dir / settings.log_file, level, json_formatter)
    error_handler = _rotating_file_handler(
        log_dir / settings.log_error_file, logging.ERROR, json_formatter
    )
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(console_formatter)
    console_handler.setLevel(level)

    for handler in (app_handler, error_handler, console_handler):
        handler.addFilter(context_filter)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.handlers.clear()
    root_logger.addHandler(app_handler)
    root_logger.addHandler(error_handler)
    root_logger.addHandler(console_handler)

    access_handler = _rotating_file_handler(
        log_dir / settings.log_access_file, level, json_formatter
    )
    access_handler.addFilter(context_filter)
    access_logger = logging.getLogger("story_ai.access")
    access_logger.setLevel(level)
    access_logger.handlers.clear()
    access_logger.addHandler(access_handler)
    access_logger.addHandler(console_handler)
    access_logger.propagate = False

    for logger_name in ("uvicorn", "uvicorn.error", "celery"):
        logger = logging.getLogger(logger_name)
        logger.setLevel(level)
        logger.propagate = True

    # RequestLoggingMiddleware emits cleaner structured access logs with request_id.
    uvicorn_access = logging.getLogger("uvicorn.access")
    uvicorn_access.handlers.clear()
    uvicorn_access.propagate = False
    uvicorn_access.disabled = True

    sqlalchemy_logger = logging.getLogger("sqlalchemy.engine")
    sqlalchemy_logger.setLevel(level if settings.log_sql_enabled else logging.WARNING)
    sqlalchemy_logger.propagate = True


def bind_request_context(request_id: Optional[str] = None, user_id: Optional[str] = None) -> None:
    if request_id:
        request_id_var.set(request_id)
    if user_id:
        user_id_var.set(user_id)


def clear_request_context() -> None:
    request_id_var.set("-")
    user_id_var.set("-")


def get_request_id() -> str:
    return request_id_var.get()


def log_extra(**kwargs: Any) -> Dict[str, Any]:
    return {"extra": _sanitize_log_value(kwargs)}


def _rotating_file_handler(
    path: Path, level: int, formatter: logging.Formatter
) -> RotatingFileHandler:
    handler = RotatingFileHandler(
        path,
        maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(formatter)
    handler.setLevel(level)
    return handler


def _sanitize_log_value(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: Dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key)
            if _is_sensitive_key(text_key):
                sanitized[text_key] = "***"
            else:
                sanitized[text_key] = _sanitize_log_value(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_log_value(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_log_value(item) for item in value]
    if isinstance(value, BaseException):
        return {
            "type": value.__class__.__name__,
            "message": str(value),
            "traceback": "".join(traceback.format_exception(value)),
        }
    return value


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(token in normalized for token in SENSITIVE_KEYS)
