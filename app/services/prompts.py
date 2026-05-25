from functools import lru_cache
from pathlib import Path

from app.core.exceptions import AppException


PROMPT_ROOT = Path(__file__).resolve().parents[1] / "prompts" / "system"
CONSTANT_PROMPT_ROOT = Path(__file__).resolve().parents[1] / "prompts" / "constants"


@lru_cache(maxsize=32)
def load_system_prompt(filename: str) -> str:
    path = PROMPT_ROOT / filename
    if not path.is_file():
        raise AppException("系统提示词不存在", code=50040, status_code=500)
    return path.read_text(encoding="utf-8")


def render_system_prompt(filename: str, **variables: str) -> str:
    prompt = load_system_prompt(filename)
    for key, value in variables.items():
        prompt = prompt.replace(f"{{{{{key}}}}}", value or "")
    return prompt


@lru_cache(maxsize=64)
def load_constant_prompt(relative_path: str) -> str:
    path = (CONSTANT_PROMPT_ROOT / relative_path).resolve()
    root = CONSTANT_PROMPT_ROOT.resolve()
    if root not in path.parents or not path.is_file():
        raise AppException("常量提示词不存在", code=50041, status_code=500)
    return path.read_text(encoding="utf-8").strip()
