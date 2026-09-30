from typing import Any, Dict, Optional


# Comfly chat completions accepts maxOutputTokens in [1, 65537).
TEXT_ANALYSIS_MAX_TOKENS = 65536
TEXT_ANALYSIS_BLOCKED_PROMPT_KEYS = {
    "content",
    "input",
    "message",
    "messages",
    "prompt",
    "system_prompt",
}


def normalize_text_analysis_extra(extra: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    normalized = {
        key: value
        for key, value in dict(extra or {}).items()
        if key not in TEXT_ANALYSIS_BLOCKED_PROMPT_KEYS
    }
    normalized["max_tokens"] = _normalize_max_tokens(normalized.get("max_tokens"))
    return normalized


def _normalize_max_tokens(value: Any) -> int:
    if value in (None, ""):
        return TEXT_ANALYSIS_MAX_TOKENS
    try:
        return min(max(1, int(value)), TEXT_ANALYSIS_MAX_TOKENS)
    except (TypeError, ValueError):
        return TEXT_ANALYSIS_MAX_TOKENS
