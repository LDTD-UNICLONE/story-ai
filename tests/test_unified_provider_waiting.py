from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.core.timezone import beijing_datetime
from app.services.generation import provider_reconciliation as recovery, provider_state
from app.services.generation.provider_polling import provider_next_poll_seconds


MEDIA_TYPES = ["image", "video", "asset_image_generate", "storyboard_image", "storyboard_video"]


@pytest.mark.parametrize("kind", MEDIA_TYPES)
def test_first_query_is_immediate_and_old_client_hint_cannot_delay_it(monkeypatch, kind):
    now = beijing_datetime()
    monkeypatch.setattr(provider_state, "beijing_datetime", lambda: now)
    monkeypatch.setattr(recovery, "beijing_datetime", lambda: now)
    task = SimpleNamespace(
        status="running", generation_type=kind, extra={"next_poll_seconds": 120},
        provider_submitted_at=None, last_reconcile_at=None,
    )
    provider_state.record_provider_task_state(task, {"task_id": "accepted", "task_status": "running"})
    assert task.next_reconcile_at == now
    assert recovery.provider_reconcile_delay_seconds(task) == 0


@pytest.mark.parametrize("offset,expected", [(-30, 0), (0, 0), (1.25, 2)])
def test_enqueue_uses_remaining_due_time_not_a_new_full_interval(monkeypatch, offset, expected):
    now = beijing_datetime()
    monkeypatch.setattr(recovery, "beijing_datetime", lambda: now)
    task = SimpleNamespace(
        generation_type="video", extra={"next_poll_seconds": 120},
        next_reconcile_at=now + timedelta(seconds=offset),
    )
    assert recovery.provider_reconcile_delay_seconds(task) == expected


@pytest.mark.parametrize("kind", MEDIA_TYPES)
def test_platform_status_reads_do_not_add_a_second_supplier_wait(kind):
    assert provider_next_poll_seconds(kind, "running", {"next_poll_seconds": 120}) == 1
    assert provider_next_poll_seconds(kind, "success", {"next_poll_seconds": 120}) is None
