from collections import deque
from pathlib import Path
from typing import Dict, List

from app.core.config import settings


def get_log_file_path() -> Path:
    return Path(settings.log_dir) / settings.log_file


def read_system_log_tail(lines: int = 300, keyword: str = "") -> Dict[str, object]:
    log_path = get_log_file_path()
    if not log_path.is_file():
        return {"path": str(log_path), "items": [], "total": 0}

    max_lines = max(1, min(lines, 2000))
    normalized_keyword = (keyword or "").strip().lower()
    buffer = deque(maxlen=max_lines)

    with log_path.open("r", encoding="utf-8", errors="ignore") as file:
        for line in file:
            value = line.rstrip("\n")
            if normalized_keyword and normalized_keyword not in value.lower():
                continue
            buffer.append(value)

    items: List[str] = list(buffer)
    return {"path": str(log_path), "items": items, "total": len(items)}
