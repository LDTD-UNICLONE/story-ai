from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from app.models.ai_model import AiModel


def calculate_model_points_cost(
    ai_model: AiModel,
    *,
    base_points: Optional[int] = None,
    use_cache_multiplier: bool = False,
    use_completion_multiplier: bool = False,
) -> int:
    points = Decimal(base_points if base_points is not None else ai_model.points_cost)
    points *= _decimal(ai_model.model_multiplier)
    if use_cache_multiplier:
        points *= _decimal(ai_model.cache_multiplier)
    if use_completion_multiplier:
        points *= _decimal(ai_model.completion_multiplier)
    points *= _decimal(ai_model.platform_multiplier)
    return max(0, int(points.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))


def _decimal(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value or "1"))
