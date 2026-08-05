import pytest

from app.core.exceptions import AppException
from app.services.task_records import _enforce_user_task_limits


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
