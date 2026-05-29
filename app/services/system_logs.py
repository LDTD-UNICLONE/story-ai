import json
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.config import settings


LOG_FILE_ALIASES = {
    "app": "log_file",
    "main": "log_file",
    "access": "log_access_file",
    "error": "log_error_file",
}


def list_log_files() -> Dict[str, str]:
    return {
        "app": str(get_log_file_path("app")),
        "access": str(get_log_file_path("access")),
        "error": str(get_log_file_path("error")),
    }


def get_log_file_path(log_file: str = "app") -> Path:
    selected = (log_file or "app").strip().lower()
    setting_name = LOG_FILE_ALIASES.get(selected)
    if setting_name:
        filename = getattr(settings, setting_name)
    else:
        filename = selected
    path = Path(settings.log_dir) / Path(filename).name
    return path


def read_system_log_tail(
    lines: int = 300,
    keyword: str = "",
    log_file: str = "app",
    level: Optional[str] = None,
    request_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Dict[str, object]:
    log_path = get_log_file_path(log_file)
    selected = (log_file or "app").strip().lower()
    if not log_path.is_file():
        return {
            "path": str(log_path),
            "selected": selected,
            "files": list_log_files(),
            "items": [],
            "raw_items": [],
            "summary": _empty_summary(),
            "total": 0,
        }

    max_lines = max(1, min(lines, 2000))
    normalized_keyword = (keyword or "").strip().lower()
    normalized_level = (level or "").strip().upper()
    normalized_request_id = (request_id or "").strip()
    normalized_user_id = (user_id or "").strip()
    buffer = deque(maxlen=max_lines)

    with log_path.open("r", encoding="utf-8", errors="ignore") as file:
        for line in file:
            raw = line.rstrip("\n")
            item = _parse_log_line(raw)
            if normalized_keyword and normalized_keyword not in raw.lower():
                continue
            if normalized_level and str(item.get("level") or "").upper() != normalized_level:
                continue
            if normalized_request_id and item.get("request_id") != normalized_request_id:
                continue
            if normalized_user_id and item.get("user_id") != normalized_user_id:
                continue
            buffer.append(item)

    items: List[Dict[str, Any]] = list(buffer)
    return {
        "path": str(log_path),
        "selected": selected,
        "files": list_log_files(),
        "items": items,
        "raw_items": [str(item.get("raw") or "") for item in items],
        "summary": _build_summary(items),
        "total": len(items),
    }


def _parse_log_line(raw: str) -> Dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return _plain_log_item(raw)
    if not isinstance(payload, dict):
        return _plain_log_item(raw)

    known_keys = {
        "timestamp",
        "level",
        "logger",
        "message",
        "request_id",
        "user_id",
        "app_name",
        "app_env",
        "module",
        "function",
        "line",
        "process",
        "thread",
        "exception",
        "stack",
        "event",
        "method",
        "path",
        "query",
        "status_code",
        "duration_ms",
        "client_ip",
        "user_agent",
        "referer",
    }
    extra = {key: value for key, value in payload.items() if key not in known_keys}
    return {
        "raw": raw,
        "parsed": True,
        "timestamp": payload.get("timestamp"),
        "level": payload.get("level"),
        "logger": payload.get("logger"),
        "message": payload.get("message"),
        "request_id": payload.get("request_id"),
        "user_id": payload.get("user_id"),
        "event": payload.get("event"),
        "method": payload.get("method"),
        "path": payload.get("path"),
        "query": payload.get("query"),
        "status_code": payload.get("status_code"),
        "duration_ms": payload.get("duration_ms"),
        "client_ip": payload.get("client_ip"),
        "user_agent": payload.get("user_agent"),
        "referer": payload.get("referer"),
        "module": payload.get("module"),
        "function": payload.get("function"),
        "line": payload.get("line"),
        "exception": payload.get("exception"),
        "extra": extra,
    }


def _plain_log_item(raw: str) -> Dict[str, Any]:
    return {
        "raw": raw,
        "parsed": False,
        "timestamp": None,
        "level": None,
        "logger": None,
        "message": raw,
        "request_id": None,
        "user_id": None,
        "event": None,
        "method": None,
        "path": None,
        "query": None,
        "status_code": None,
        "duration_ms": None,
        "client_ip": None,
        "user_agent": None,
        "referer": None,
        "module": None,
        "function": None,
        "line": None,
        "exception": None,
        "extra": {},
    }


def _build_summary(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    summary = _empty_summary()
    for item in items:
        level = str(item.get("level") or "UNKNOWN").upper()
        summary["level_counts"][level] = summary["level_counts"].get(level, 0) + 1

        status_code = item.get("status_code")
        if status_code not in (None, ""):
            status_key = str(status_code)
            summary["status_code_counts"][status_key] = summary["status_code_counts"].get(status_key, 0) + 1
            try:
                if int(status_code) >= 400:
                    summary["error_count"] += 1
            except (TypeError, ValueError):
                pass

        duration_ms = item.get("duration_ms")
        try:
            if duration_ms is not None and float(duration_ms) >= 1000:
                summary["slow_request_count"] += 1
        except (TypeError, ValueError):
            pass

        if item.get("exception"):
            summary["exception_count"] += 1
    return summary


def _empty_summary() -> Dict[str, Any]:
    return {
        "level_counts": {},
        "status_code_counts": {},
        "error_count": 0,
        "slow_request_count": 0,
        "exception_count": 0,
    }
