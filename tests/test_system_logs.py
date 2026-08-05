from types import SimpleNamespace

import pytest

from app.api.v1.endpoints.admin import system_logs as system_logs_endpoint


@pytest.mark.asyncio
async def test_admin_system_logs_offloads_file_read(monkeypatch) -> None:
    captured = {}

    async def fake_run_in_threadpool(func, *args, **kwargs):
        captured["func"] = func
        captured["args"] = args
        captured["kwargs"] = kwargs
        return {"items": []}

    monkeypatch.setattr(
        system_logs_endpoint, "run_in_threadpool", fake_run_in_threadpool
    )

    await system_logs_endpoint.admin_system_logs(
        lines=300,
        keyword=None,
        log_file="access",
        level=None,
        request_id=None,
        user_id=None,
        current_admin=SimpleNamespace(),
    )

    assert captured == {
        "func": system_logs_endpoint.read_system_log_tail,
        "args": (),
        "kwargs": {
            "lines": 300,
            "keyword": "",
            "log_file": "access",
            "level": None,
            "request_id": None,
            "user_id": None,
        },
    }
