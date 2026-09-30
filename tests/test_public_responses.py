import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.api.v1.endpoints import projects
from app.core.public_messages import sanitize_public_data, sanitize_public_message
from app.core.responses import error, success
from app.core.timezone import beijing_datetime
from app.schemas.project import ProjectCreateRequest
from app.schemas.project_canvas import CanvasNodeOut


@pytest.mark.parametrize("value", ["", " ", "\n\t", None, [], {}, 0, False])
def test_response_data_preserves_unfilled_values(value):
    payload = {"description": value, "nodes": [{"content": {"text": value}}]}
    assert success(data=payload).model_dump(mode="json")["data"] == payload
    assert json.loads(error(data=payload).body)["data"] == payload


@pytest.mark.asyncio
async def test_create_project_response_keeps_omitted_fields_empty(monkeypatch):
    payload = ProjectCreateRequest(name="新项目")
    user = SimpleNamespace(id=uuid4())
    project = SimpleNamespace(
        id=uuid4(), user_id=user.id, name=payload.name, cover=payload.cover,
        description=payload.description, project_kind="standard", is_enabled=True,
        created_at=beijing_datetime(), updated_at=beijing_datetime(),
    )
    create = AsyncMock(return_value=project)
    monkeypatch.setattr(projects, "create_project", create)
    response = await projects.create_my_project(payload, db=None, current_user=user)
    data = response.model_dump(mode="json")["data"]
    assert response.code == 0 and response.message == "创建成功"
    assert data["cover"] == data["description"] == ""
    assert project.cover == project.description == ""
    create.assert_awaited_once_with(None, user, payload)


@pytest.mark.parametrize("kind", ["text", "image", "video", "group"])
def test_unfilled_canvas_node_response_does_not_invent_model_failure(kind):
    node = CanvasNodeOut(id=uuid4(), kind=kind, content_revision=1)
    data = success(data={"nodes": [node.model_dump(mode="json")]}).data["nodes"][0]
    assert data["title"] == data["content"]["text"] == ""
    assert data["content"]["generation"] is None
    assert data["latest_generation_id"] is None
    assert data["selected_generation_id"] is None


def test_preserving_empty_data_keeps_actual_errors_sanitized():
    data = sanitize_public_data({
        "description": "",
        "extra": {
            "failed_reason": "apimart HTTP 429 request_id=test",
            "raw_failed_reason": "private error",
            "provider_response": {"private": "response"},
            "provider_cost_billing": {"cost": 10},
        },
        "vendor": "apimart",
    })
    assert data == {
        "description": "", "extra": {"failed_reason": "模型服务繁忙，请稍后再试"},
        "vendor": "apimart",
    }
    assert sanitize_public_message("") == "模型响应失败，请稍后再试"
    assert sanitize_public_message(" ", fallback="请求失败") == "请求失败"
