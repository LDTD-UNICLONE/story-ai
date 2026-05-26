from typing import Any, Dict, Optional


TEXT_ANALYSIS_MAX_TOKENS = 100000


def normalize_text_analysis_extra(extra: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    normalized = dict(extra or {})
    normalized["max_tokens"] = _normalize_max_tokens(normalized.get("max_tokens"))
    return normalized


def _normalize_max_tokens(value: Any) -> int:
    if value in (None, ""):
        return TEXT_ANALYSIS_MAX_TOKENS
    try:
        return min(max(1, int(value)), TEXT_ANALYSIS_MAX_TOKENS)
    except (TypeError, ValueError):
        return TEXT_ANALYSIS_MAX_TOKENS
