from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.config import settings
from app.services.generation.provider_budget import retry_delay, retry_after_seconds, account_scope
from app.services.generation.task_events import task_phase


def test_retry_after_survives_wrapped_supplier_error_and_overrides_local_cap():
    source = RuntimeError("rate limited")
    source.response = SimpleNamespace(headers={"Retry-After": "120"})
    wrapped = RuntimeError("public message")
    wrapped.__cause__ = source
    assert retry_delay(1, wrapped) == 120
    source.response.headers["Retry-After"] = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=90))
    assert 88 <= retry_after_seconds(wrapped) <= 90


def test_backoff_is_bounded_and_increases():
    assert 2 <= retry_delay(1) <= 4
    assert 16 <= retry_delay(4) <= 18
    assert retry_delay(100) == settings.provider_query_retry_max_seconds


def test_scope_is_shared_by_vendor_alias_not_model_and_hides_credential(monkeypatch):
    secret = uuid4().hex
    monkeypatch.setattr(settings, "comfly_api_key", secret)
    assert account_scope("comfly") == account_scope("模型服务")
    assert secret not in account_scope("comfly")
    first = account_scope("comfly")
    monkeypatch.setattr(settings, "comfly_api_key", "another-account")
    assert account_scope("comfly") != first


@pytest.mark.parametrize("status,extra,expected", [
    ("pending", {}, "queued"), ("running", {}, "generating"),
    ("running", {"generation_phase": "persisting"}, "persisting"),
    ("success", {"generation_phase": "persisting"}, "completed"),
    ("failed", {"generation_phase": "persisting"}, "failed"),
])
def test_phase_keeps_existing_terminal_status_authoritative(status, extra, expected):
    assert task_phase(status, extra) == expected
