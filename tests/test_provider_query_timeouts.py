from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

from app.core.config import settings
from app.integrations import apimart, comfly, volcengine_ark


async def test_apimart_query_has_own_timeout_and_no_sdk_retry(monkeypatch):
    response = httpx.Response(200, json={"data": {"status": "running"}}, request=httpx.Request("GET", "https://example.com"))
    client = SimpleNamespace(get=AsyncMock(return_value=response))
    monkeypatch.setattr(apimart, "_get_client", AsyncMock(return_value=client))
    assert (await apimart.query_generation_task("task"))["status"] == "running"
    options = client.get.call_args.kwargs["options"]
    assert options["timeout"] == settings.provider_query_timeout_seconds
    assert options["max_retries"] == 0


async def test_comfly_query_has_own_timeout(monkeypatch):
    response = httpx.Response(200, json={"status": "running"}, request=httpx.Request("GET", "https://example.com"))
    client = SimpleNamespace(get=AsyncMock(return_value=response))
    monkeypatch.setattr(comfly, "_get_client", AsyncMock(return_value=client))
    monkeypatch.setattr(settings, "comfly_base_url", "https://example.com")
    monkeypatch.setattr(settings, "comfly_api_key", "test-only")
    await comfly.query_image_generation("task")
    assert client.get.call_args.kwargs["timeout"] == settings.provider_query_timeout_seconds


async def test_ark_query_uses_separate_client_and_closes_it(monkeypatch):
    calls = []
    client = SimpleNamespace(close=lambda: calls.append("closed"))
    def create(*, query=False):
        assert query
        return client
    monkeypatch.setattr(volcengine_ark, "_create_client", create)
    monkeypatch.setattr(volcengine_ark, "_get_video_task", lambda instance, task: {"id": task, "status": "running"})
    assert (await volcengine_ark.query_video_generation("task"))["id"] == "task"
    assert calls == ["closed"]


def test_ark_query_constructor_disables_internal_retries(monkeypatch):
    import sys
    calls = []
    monkeypatch.setattr(settings, "volcengine_ark_api_key", "test-only")
    monkeypatch.setitem(sys.modules, "volcenginesdkarkruntime", SimpleNamespace(Ark=lambda **kwargs: calls.append(kwargs)))
    volcengine_ark._create_client(query=True)
    assert calls[0]["timeout"] == settings.provider_query_timeout_seconds
    assert calls[0]["max_retries"] == 0
