import pytest

from app.core.config import settings
from app.services.generation.provider_polling import (
    defer_provider_task_result,
    provider_poll_interval_seconds,
)
from app.services.generation.runner import ModelRunResult


@pytest.mark.parametrize("kind", ["image", "video", "asset_image_generate", "storyboard_image", "storyboard_video"])
def test_all_media_use_one_configured_query_interval(monkeypatch, kind):
    monkeypatch.setattr(settings, "provider_task_poll_interval_seconds", 3)
    assert provider_poll_interval_seconds(kind) == 3


@pytest.mark.parametrize("interval", [0, -1])
def test_query_interval_never_busy_loops(monkeypatch, interval):
    monkeypatch.setattr(settings, "provider_task_poll_interval_seconds", interval)
    assert provider_poll_interval_seconds("image") == 1


def test_handoff_preserves_provider_metadata_without_querying():
    result = ModelRunResult("生成任务处理中", {"task_id": "provider-1", "task_status": "queued", "progress": 42})
    assert defer_provider_task_result(result, "provider-1") is result
    assert result.content == "模型任务仍在生成中：provider-1"
    assert result.extra == {
        "task_id": "provider-1", "task_status": "queued", "progress": 42,
        "platform_task_status": "running", "provider_polling_deferred": True,
        "next_poll_seconds": 1,
    }
