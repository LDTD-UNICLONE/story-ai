from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.api.v1.endpoints.task_polling import task_next_poll_seconds
from app.core.config import settings
from app.core.timezone import beijing_datetime
from app.services.generation import provider_reconciliation, provider_state
from app.services.generation.provider_polling import provider_next_poll_seconds
from app.services.generation.provider_polling import provider_poll_interval_seconds
from app.tasks import provider_reconcile


@pytest.mark.parametrize("kind", ["image", "video", "asset_image_generate", "storyboard_image", "storyboard_video"])
def test_19_second_media_is_detected_by_20_seconds_and_visible_by_21(monkeypatch, kind):
    """Replay the real due-time, Celery countdown and client hint without sleeping."""
    now = beijing_datetime()
    submitted_at = now
    monkeypatch.setattr(provider_state, "beijing_datetime", lambda: now)
    monkeypatch.setattr(provider_reconciliation, "beijing_datetime", lambda: now)
    task = SimpleNamespace(
        id=uuid4(), status="running", generation_type=kind, extra={},
        business_type="conversation" if kind in {"image", "video"} else "project", provider_task_id=None, provider_submitted_at=None, last_reconcile_at=None,
    )
    countdowns = []
    monkeypatch.setattr(
        provider_reconcile.reconcile_provider_task, "apply_async",
        lambda **kwargs: countdowns.append(kwargs["countdown"]),
    )
    for _ in range(12):
        provider_state.record_provider_task_state(task, {"task_id": "19-second-image", "task_status": "running"}, "apimart")
        assert provider_reconcile.enqueue_provider_reconcile_best_effort(task)
        next_query = now + timedelta(seconds=countdowns[-1])
        assert next_query == task.next_reconcile_at
        now = next_query
        task.last_reconcile_at = now
        if (now - submitted_at).total_seconds() >= 19:
            break
    detected = (now - submitted_at).total_seconds()
    visible = detected + task_next_poll_seconds(task)
    assert detected <= 20, f"19-second image first observed at {detected}s"
    assert visible <= 21, f"Client may wait until {visible}s"
    assert countdowns[0] == 0
    assert len(countdowns) == 11


@pytest.mark.parametrize("kind", ["image", "asset_image_generate", "storyboard_image"])
def test_old_image_hint_cannot_add_another_minute(kind):
    assert provider_next_poll_seconds(kind, "running", {"next_poll_seconds": 60}) == 1


@pytest.mark.parametrize("status", ["success", "failed"])
def test_finished_image_stops_polling(status):
    assert provider_next_poll_seconds("image", status, {"next_poll_seconds": 60}) is None


def test_explicit_supplier_interval_does_not_slow_client_reads(monkeypatch):
    monkeypatch.setattr(settings, "provider_task_poll_interval_seconds", 60)
    assert provider_poll_interval_seconds("image") == 60
    assert provider_next_poll_seconds("image", "running") == 1


@pytest.mark.parametrize("kind", ["text", "video"])
def test_other_generation_types_also_use_short_platform_poll_hint(kind):
    assert provider_next_poll_seconds(kind, "running", {"next_poll_seconds": 17}) == 1
