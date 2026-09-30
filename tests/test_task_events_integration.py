# ruff: noqa: F811
import json
import os
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_current_user
from app.api.v1.endpoints import task_records
from app.core.exceptions import register_exception_handlers
from app.db.session import get_db
from app.models.task_record import UserTaskRecord
from app.models.user import User
from tests.test_task_lifecycle_integration import lifecycle_db, _seed_task  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.skipif(os.getenv("RUN_DB_INTEGRATION_TESTS") != "1", reason="isolated database")]


@asynccontextmanager
async def no_subscription(_user_id):
    yield None


async def test_stream_route_auth_ownership_phase_and_fallback(lifecycle_db, monkeypatch):
    mine = await _seed_task(lifecycle_db, "image")
    foreign = await _seed_task(lifecycle_db, "video")
    async with lifecycle_db() as db:
        user = await db.get(User, mine.user_id)
        task = await db.get(UserTaskRecord, mine.task_id)
        task.extra = {**task.extra, "generation_phase": "persisting", "provider_completed_result": {"content": "PRIVATE_PROVIDER_URL", "extra": {}}}
        await db.commit()
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(task_records.router, prefix="/api/v1")
    async def database():
        async with lifecycle_db() as db:
            yield db
    app.dependency_overrides[get_db] = database
    monkeypatch.setattr(task_records, "AsyncSessionLocal", lifecycle_db)
    monkeypatch.setattr(task_records, "task_change_subscription", no_subscription)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        path = "/api/v1/task-records/stream"
        assert (await client.get(path, params={"ids": str(mine.task_id)})).status_code == 401
        app.dependency_overrides[get_current_user] = lambda: user
        response = await client.get(path, params=[("ids", str(mine.task_id)), ("ids", str(foreign.task_id))])
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        payload = json.loads(response.text.splitlines()[1].removeprefix("data: "))
        assert payload["items"] == [{"task_record_id": str(mine.task_id), "status": "running", "phase": "persisting", "progress_percent": None}]
        assert payload["missing_ids"] == [str(foreign.task_id)]
        assert "event: fallback" in response.text
        assert "PRIVATE_PROVIDER_URL" not in response.text
        assert (await client.get(path)).status_code == 422
        assert (await client.get(path, params=[("ids", str(uuid4())) for _ in range(51)])).status_code == 422
        detail = await client.get(f"/api/v1/task-records/{mine.task_id}")
        assert detail.json()["data"]["phase"] == "persisting"
        assert "provider_completed_result" not in detail.json()["data"]["extra"]


async def test_stream_rechecks_snapshot_after_notification_and_stops_at_terminal(lifecycle_db, monkeypatch):
    seed = await _seed_task(lifecycle_db, "image")
    @asynccontextmanager
    async def subscribed(_user):
        yield object()
    waits = []
    async def wait(subscription, ids):
        waits.append(ids)
        async with lifecycle_db() as db:
            task = await db.get(UserTaskRecord, seed.task_id)
            task.status = "success"
            await db.commit()
        return True
    monkeypatch.setattr(task_records, "AsyncSessionLocal", lifecycle_db)
    monkeypatch.setattr(task_records, "task_change_subscription", subscribed)
    monkeypatch.setattr(task_records, "wait_for_task_change", wait)
    class Request:
        async def is_disconnected(self):
            return False
    events = [event async for event in task_records._task_record_events(Request(), seed.user_id, [seed.task_id])]
    assert len(waits) == 1 and len(events) == 2
    final = json.loads(events[-1].splitlines()[1].removeprefix("data: "))
    assert final["items"][0]["phase"] == "completed" and final["stop_streaming"]
