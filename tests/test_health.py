import json

from fastapi.responses import JSONResponse

from app.api.v1.endpoints import health
from app.core.responses import ApiResponse


class FakeSession:
    def __init__(self, *, fails: bool = False) -> None:
        self.fails = fails

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    async def execute(self, statement) -> None:
        if self.fails:
            raise RuntimeError("database unavailable")


class FakeRedis:
    def __init__(self, *, available: bool) -> None:
        self.available = available

    async def ping(self) -> bool:
        return self.available


async def test_health_reports_ready_when_dependencies_are_available(monkeypatch) -> None:
    monkeypatch.setattr(health, "AsyncSessionLocal", lambda: FakeSession())
    monkeypatch.setattr(health, "get_redis", lambda: FakeRedis(available=True))

    response = await health.health_check()

    assert isinstance(response, ApiResponse)
    assert response.data["status"] == "ok"
    assert response.data["database"] is True
    assert response.data["redis"] is True


async def test_health_returns_503_when_a_dependency_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(health, "AsyncSessionLocal", lambda: FakeSession(fails=True))
    monkeypatch.setattr(health, "get_redis", lambda: FakeRedis(available=True))

    response = await health.health_check()

    assert isinstance(response, JSONResponse)
    assert response.status_code == 503
    payload = json.loads(response.body)
    assert payload["data"]["status"] == "unavailable"
    assert payload["data"]["database"] is False
