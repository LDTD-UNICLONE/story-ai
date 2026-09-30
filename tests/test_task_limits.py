from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.core.exceptions import AppException
from app.services.generation.task_records import _enforce_user_task_limits, _lock_user_task_submission


def test_media_task_limit_blocks_submission() -> None:
    snapshot = {
        "configured_media_task_limit": 5,
        "exceeds_configured_media_task_limit": True,
        "exceeds_configured_task_limit": False,
    }

    with pytest.raises(AppException) as exc_info:
        _enforce_user_task_limits(snapshot)

    assert exc_info.value.status_code == 429


def test_task_below_limits_is_allowed() -> None:
    _enforce_user_task_limits(
        {
            "exceeds_configured_media_task_limit": False,
            "exceeds_configured_task_limit": False,
        }
    )


@pytest.mark.asyncio
async def test_outstanding_actual_cost_blocks_new_model_task() -> None:
    user = SimpleNamespace(points_balance=-1)
    result = SimpleNamespace(scalar_one_or_none=lambda: user)
    db = SimpleNamespace(execute=AsyncMock(return_value=result))

    with pytest.raises(AppException) as exc_info:
        await _lock_user_task_submission(db, uuid4())

    assert exc_info.value.code == 40003
    assert "未结清" in exc_info.value.message
