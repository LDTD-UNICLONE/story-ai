import warnings
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.api.deps import get_current_user
from app.api.v1.endpoints import agent_productions
from app.core.exceptions import register_exception_handlers
from app.core.timezone import beijing_datetime
from app.db.session import get_db


@pytest.fixture
async def agent_delete_api(monkeypatch):
    production_id = uuid4()
    user = SimpleNamespace(id=uuid4())
    now = beijing_datetime()
    item = dict(
        id=production_id,
        name="Test",
        status="draft",
        current_stage="source",
        mode="supervised",
        source_type="text",
        character_count=10,
        configuration_required=True,
        consumed_points=0,
        active_task_count=0,
        has_active_tasks=False,
        should_poll=False,
        created_at=now,
        updated_at=now,
    )
    monkeypatch.setattr(
        agent_productions, "list_agent_projects", AsyncMock(return_value=([item], 1))
    )
    delete = AsyncMock(
        return_value=dict(production_id=production_id, status="cancelled", deleted=True)
    )
    monkeypatch.setattr(agent_productions, "delete_agent_production", delete)
    app = FastAPI()
    app.include_router(agent_productions.router, prefix="/api/v1")
    register_exception_handlers(app)
    app.dependency_overrides[get_db] = lambda: None
    app.dependency_overrides[get_current_user] = lambda: user
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield SimpleNamespace(client=client, production_id=production_id, delete=delete, user=user)


async def test_invalid_production_id_never_calls_delete_or_emits_deprecation(agent_delete_api):
    ctx = agent_delete_api
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        response = await ctx.client.delete("/api/v1/agent-productions/undefined")
    assert response.status_code == 422
    assert response.json()["code"] == 42200
    assert response.json()["data"][0]["loc"] == ["path", "production_id"]
    ctx.delete.assert_not_awaited()
    assert not [item for item in captured if "HTTP_422_UNPROCESSABLE_ENTITY" in str(item.message)]


async def test_list_item_id_is_used_as_delete_path_production_id(agent_delete_api):
    ctx = agent_delete_api
    response = await ctx.client.get("/api/v1/agent-productions")
    assert response.status_code == 200, response.text
    item = response.json()["data"]["items"][0]
    assert item["id"] == str(ctx.production_id)
    assert "production_id" not in item
    response = await ctx.client.delete(f"/api/v1/agent-productions/{item['id']}")
    assert response.status_code == 200, response.text
    assert response.json()["data"] == dict(
        production_id=item["id"], status="cancelled", deleted=True
    )
    ctx.delete.assert_awaited_once_with(None, ctx.production_id, ctx.user)
