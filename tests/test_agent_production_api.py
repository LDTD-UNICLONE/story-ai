from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.core.exceptions import AppException
from app.main import app
from app.models.agent_production import AgentEvent, AgentProduction, AgentStep
from app.schemas.agent_production import (
    AgentProductionConfigurationRequest,
    AgentProductionCreateRequest,
    AgentProductionFromTextRequest,
)
from app.services import agent_productions as production_service
from app.services import agent_entries as entry_service
from app.services.agent_entries import agent_list_polling_state


class FakeScalars:
    def __init__(self, items):
        self.items = items

    def all(self):
        return self.items


class FakeResult:
    def __init__(self, scalar=None, items=None):
        self.scalar = scalar
        self.items = items or []

    def scalar_one(self):
        return self.scalar

    def scalar_one_or_none(self):
        return self.scalar

    def scalars(self):
        return FakeScalars(self.items)


class FakeSession:
    def __init__(self, results=None):
        self.results = list(results or [])
        self.added = []
        self.commits = 0

    async def execute(self, _statement):
        return self.results.pop(0)

    def add(self, item):
        self.added.append(item)

    async def commit(self):
        self.commits += 1

    async def get(self, _model, _identifier):
        return None


def production(status: str = "draft") -> AgentProduction:
    return AgentProduction(
        id=uuid4(),
        project_id=uuid4(),
        user_id=uuid4(),
        source_document_id=uuid4(),
        status=status,
        current_stage="source",
        mode="supervised",
        production_spec={
            "text_model_id": str(uuid4()),
            "image_model_id": str(uuid4()),
            "video_model_id": str(uuid4()),
        },
        estimated_points=0,
        consumed_points=0,
        max_points=None,
        lock_version=0,
        extra={},
    )


def test_agent_list_polling_follows_real_background_work() -> None:
    supervised = production("planning")
    supervised.current_stage = "script_review"
    assert agent_list_polling_state(supervised, 0) == {
        "has_active_tasks": False,
        "should_poll": False,
        "next_poll_seconds": None,
    }
    assert agent_list_polling_state(supervised, 1) == {
        "has_active_tasks": True,
        "should_poll": True,
        "next_poll_seconds": 10,
    }

    automatic = production("running")
    automatic.mode = "automatic"
    automatic.current_stage = "batch_storyboards"
    assert agent_list_polling_state(automatic, 0)["should_poll"] is True

    automatic.status = "partially_failed"
    assert agent_list_polling_state(automatic, 0)["should_poll"] is False


def production_payload(**overrides):
    data = {
        "content": "第一集：雨夜相遇",
        "production_spec": {
            "text_model_id": uuid4(),
            "image_model_id": uuid4(),
            "video_model_id": uuid4(),
        },
    }
    data.update(overrides)
    return data


def test_agent_production_routes_are_registered() -> None:
    routes = {
        (path, method.upper())
        for path, methods in app.openapi()["paths"].items()
        for method in methods
    }

    assert ("/api/v1/projects/{project_id}/agent-productions", "POST") in routes
    assert ("/api/v1/agent-productions/from-file", "POST") in routes
    assert ("/api/v1/agent-productions/from-text", "POST") in routes
    assert ("/api/v1/agent-productions/{production_id}/configuration", "GET") in routes
    assert ("/api/v1/agent-productions/{production_id}/configuration", "PUT") in routes
    assert ("/api/v1/agent-productions", "GET") in routes
    assert (
        "/api/v1/projects/{project_id}/agent-productions/source-preview",
        "POST",
    ) in routes
    assert ("/api/v1/projects/{project_id}/agent-productions", "GET") in routes
    assert ("/api/v1/agent-productions/{production_id}", "GET") in routes
    assert ("/api/v1/agent-productions/{production_id}", "DELETE") in routes
    assert ("/api/v1/agent-productions/{production_id}/workbench", "GET") in routes
    assert ("/api/v1/agent-productions/{production_id}/review", "GET") in routes
    assert ("/api/v1/agent-productions/{production_id}/review-issues", "POST") in routes
    assert (
        "/api/v1/agent-productions/{production_id}/review-issues/{issue_id}",
        "PATCH",
    ) in routes
    assert (
        "/api/v1/agent-productions/{production_id}/episodes/{chapter_id}/video-timeline",
        "GET",
    ) in routes
    assert ("/api/v1/agent-productions/{production_id}/jianying-exports", "POST") in routes
    assert ("/api/v1/agent-productions/{production_id}/jianying-exports", "GET") in routes
    assert (
        "/api/v1/agent-productions/{production_id}/jianying-exports/{export_id}",
        "GET",
    ) in routes
    assert (
        "/api/v1/agent-productions/{production_id}/episodes/{chapter_id}/approve",
        "POST",
    ) in routes
    assert (
        "/api/v1/agent-productions/{production_id}/delivery-readiness",
        "GET",
    ) in routes
    assert ("/api/v1/agent-productions/{production_id}/deliveries", "POST") in routes
    assert ("/api/v1/agent-productions/{production_id}/deliveries", "GET") in routes
    assert (
        "/api/v1/agent-productions/{production_id}/deliveries/{delivery_id}",
        "GET",
    ) in routes
    for action in ("start", "pause", "resume", "cancel"):
        assert (f"/api/v1/agent-productions/{{production_id}}/{action}", "POST") in routes
    assert ("/api/v1/agent-productions/{production_id}/episode-plans", "GET") in routes
    assert (
        "/api/v1/agent-productions/{production_id}/episode-plans/{plan_id}",
        "PATCH",
    ) in routes
    assert ("/api/v1/agent-productions/{production_id}/episode-plans/merge", "POST") in routes
    assert (
        "/api/v1/agent-productions/{production_id}/episode-plans/{plan_id}/split",
        "POST",
    ) in routes
    assert (
        "/api/v1/agent-productions/{production_id}/episode-plans/impact-preview",
        "GET",
    ) in routes
    assert ("/api/v1/agent-productions/{production_id}/episode-plans/confirm", "POST") in routes


def test_agent_production_payload_applies_safe_defaults() -> None:
    payload = AgentProductionCreateRequest.model_validate(production_payload())

    assert payload.mode == "supervised"
    assert payload.production_spec.pilot_episode_count == 1
    assert payload.production_spec.default_shot_duration_seconds == 5


def test_new_agent_entry_accepts_only_initial_project_configuration() -> None:
    style_id = uuid4()
    video_model_id = uuid4()
    payload = {
        "name": "雨夜旧宅",
        "content": "第一集：雨夜相遇",
        "style_id": style_id,
        "generation_ratio": "9:16",
        "video_resolution": "1080p",
        "mode": "automatic",
        "video_model_id": video_model_id,
    }
    parsed = AgentProductionFromTextRequest.model_validate(payload)

    assert parsed.style_id == style_id
    assert parsed.generation_ratio == "9:16"
    assert parsed.video_resolution == "1080p"
    assert parsed.mode == "automatic"
    assert parsed.video_model_id == video_model_id
    for forbidden_field in (
        "text_model_id",
        "image_model_id",
        "retry_limit",
        "pilot_episode_count",
        "max_points",
        "production_options",
        "generate_audio",
        "target_episode_count",
        "target_episode_duration_seconds",
        "default_shot_duration_seconds",
    ):
        invalid = {**payload, forbidden_field: 1}
        with pytest.raises(ValidationError):
            AgentProductionFromTextRequest.model_validate(invalid)

    for field, value in (
        ("generation_ratio", "2:1"),
        ("video_resolution", "2k"),
        ("mode", "manual"),
    ):
        invalid_options = {**payload, field: value}
        with pytest.raises(ValidationError):
            AgentProductionFromTextRequest.model_validate(invalid_options)

    configuration = AgentProductionConfigurationRequest.model_validate(
        {
            "style_id": uuid4(),
            "generation_ratio": "9:16",
            "video_resolution": "1080p",
            "mode": "automatic",
            "video_model_id": video_model_id,
        }
    )
    assert configuration.mode == "automatic"
    assert configuration.video_model_id == video_model_id

    with pytest.raises(ValidationError):
        AgentProductionConfigurationRequest.model_validate(
            {
                "style_id": uuid4(),
                "generation_ratio": "2:1",
                "video_resolution": "1080p",
                "mode": "automatic",
                "video_model_id": video_model_id,
            }
        )


def test_automatic_agent_entry_requires_video_model() -> None:
    payload = {
        "content": "第一集：雨夜相遇",
        "style_id": uuid4(),
        "generation_ratio": "9:16",
        "video_resolution": "1080p",
        "mode": "automatic",
    }

    with pytest.raises(ValidationError):
        AgentProductionFromTextRequest.model_validate(payload)

    with pytest.raises(ValidationError):
        AgentProductionConfigurationRequest.model_validate(
            {key: value for key, value in payload.items() if key != "content"}
        )


def test_supervised_agent_entry_defers_video_model_selection() -> None:
    payload = {
        "content": "第一集：雨夜相遇",
        "style_id": uuid4(),
        "generation_ratio": "9:16",
        "video_resolution": "1080p",
        "mode": "supervised",
    }

    parsed = AgentProductionFromTextRequest.model_validate(payload)

    assert parsed.video_model_id is None
    with pytest.raises(ValidationError):
        AgentProductionFromTextRequest.model_validate(
            {**payload, "video_model_id": uuid4()}
        )


@pytest.mark.asyncio
async def test_file_entry_validates_automatic_video_model(monkeypatch) -> None:
    model_id = uuid4()
    model = SimpleNamespace(id=model_id)

    async def get_video_model(_db, value):
        assert value == model_id
        return model

    monkeypatch.setattr(entry_service, "get_agent_video_model", get_video_model)

    assert await entry_service._initial_video_model(
        SimpleNamespace(), "automatic", model_id
    ) is model
    with pytest.raises(AppException) as missing:
        await entry_service._initial_video_model(SimpleNamespace(), "automatic", None)
    assert missing.value.code == 40059
    with pytest.raises(AppException) as supervised:
        await entry_service._initial_video_model(
            SimpleNamespace(), "supervised", model_id
        )
    assert supervised.value.code == 40059


@pytest.mark.asyncio
async def test_automatic_mode_requires_100_points_without_deducting(monkeypatch) -> None:
    checked = []

    async def ensure_points(_db, user_id, amount):
        checked.append((user_id, amount))

    monkeypatch.setattr(entry_service, "ensure_user_points_enough", ensure_points)
    user_id = uuid4()

    await entry_service.require_agent_automatic_mode_points(
        SimpleNamespace(), user_id, "automatic"
    )
    await entry_service.require_agent_automatic_mode_points(
        SimpleNamespace(), user_id, "supervised"
    )

    assert checked == [(user_id, 100)]


def test_agent_production_payload_normalizes_source_content() -> None:
    payload = AgentProductionCreateRequest.model_validate(
        production_payload(content="  第一集：雨夜相遇  ")
    )

    assert payload.content == "第一集：雨夜相遇"


def test_agent_production_payload_accepts_previewed_source_document() -> None:
    source_document_id = uuid4()
    payload = production_payload(source_document_id=source_document_id)
    payload.pop("content")

    parsed = AgentProductionCreateRequest.model_validate(payload)

    assert parsed.source_document_id == source_document_id
    assert parsed.content is None


def test_agent_production_payload_requires_one_source() -> None:
    payload = production_payload()
    payload.pop("content")

    with pytest.raises(ValidationError):
        AgentProductionCreateRequest.model_validate(payload)


def test_agent_production_payload_rejects_blank_source_content() -> None:
    with pytest.raises(ValidationError):
        AgentProductionCreateRequest.model_validate(production_payload(content="   "))


def test_agent_production_payload_requires_distinct_model_records() -> None:
    model_id = uuid4()
    payload = production_payload()
    payload["production_spec"]["image_model_id"] = model_id
    payload["production_spec"]["video_model_id"] = model_id

    with pytest.raises(ValidationError):
        AgentProductionCreateRequest.model_validate(payload)


@pytest.mark.parametrize(
    "production_spec",
    [
        {"pilot_episode_count": 4},
        {"default_shot_duration_seconds": 16},
        {"target_episode_count": 0},
    ],
)
def test_agent_production_payload_rejects_invalid_limits(production_spec) -> None:
    payload = production_payload()
    payload["production_spec"].update(production_spec)

    with pytest.raises(ValidationError):
        AgentProductionCreateRequest.model_validate(payload)


@pytest.mark.asyncio
async def test_start_action_queues_one_source_analysis_step(monkeypatch) -> None:
    item = production()
    user = SimpleNamespace(id=item.user_id)
    db = FakeSession(results=[FakeResult(scalar=1), FakeResult(scalar=None)])

    async def get_locked(_db, _production_id, _user_id):
        return item

    async def get_detail(_db, _production_id, _user_id):
        return item

    monkeypatch.setattr(production_service, "_get_locked_production", get_locked)
    monkeypatch.setattr(production_service, "get_agent_production_or_404", get_detail)

    result = await production_service.apply_agent_production_action(db, item.id, user, "start")

    assert result.status == "planning"
    assert result.current_stage == "source_analysis"
    assert result.lock_version == 1
    steps = [added for added in db.added if isinstance(added, AgentStep)]
    events = [added for added in db.added if isinstance(added, AgentEvent)]
    assert len(steps) == 1
    assert steps[0].status == "queued"
    assert events[0].event_type == "production.started"


@pytest.mark.asyncio
async def test_start_action_is_idempotent_after_planning_begins(monkeypatch) -> None:
    item = production(status="planning")
    user = SimpleNamespace(id=item.user_id)
    db = FakeSession()

    async def get_production(_db, _production_id, _user_id):
        return item

    monkeypatch.setattr(production_service, "_get_locked_production", get_production)
    monkeypatch.setattr(production_service, "get_agent_production_or_404", get_production)

    await production_service.apply_agent_production_action(db, item.id, user, "start")

    assert db.added == []
    assert item.lock_version == 0
    assert db.commits == 1


@pytest.mark.asyncio
async def test_completed_production_cannot_be_started_again(monkeypatch) -> None:
    item = production(status="completed")
    user = SimpleNamespace(id=item.user_id)
    db = FakeSession()

    async def get_locked(_db, _production_id, _user_id):
        return item

    monkeypatch.setattr(production_service, "_get_locked_production", get_locked)

    with pytest.raises(AppException) as exc_info:
        await production_service.apply_agent_production_action(db, item.id, user, "start")

    assert exc_info.value.status_code == 409
    assert item.status == "completed"
    assert item.current_stage != "source_analysis"


@pytest.mark.asyncio
async def test_resume_does_not_bypass_approval_checkpoint(monkeypatch) -> None:
    item = production(status="waiting_approval")
    user = SimpleNamespace(id=item.user_id)
    db = FakeSession()

    async def get_locked(_db, _production_id, _user_id):
        return item

    monkeypatch.setattr(production_service, "_get_locked_production", get_locked)

    with pytest.raises(AppException) as exc_info:
        await production_service.apply_agent_production_action(db, item.id, user, "resume")

    assert exc_info.value.status_code == 409
    assert item.status == "waiting_approval"


@pytest.mark.asyncio
async def test_resume_requeues_failed_source_analysis_without_recreating_successes(
    monkeypatch,
) -> None:
    item = production(status="partially_failed")
    user = SimpleNamespace(id=item.user_id)
    step = AgentStep(
        id=uuid4(),
        production_id=item.id,
        stage="source_analysis",
        scope_type="production",
        scope_id=item.id,
        status="failed",
        input_version=1,
        extra={"initialized": True, "chunks": []},
    )
    db = FakeSession(results=[FakeResult(scalar=step)])
    enqueued = []

    async def get_locked(_db, _production_id, _user_id):
        return item

    async def get_detail(_db, _production_id, _user_id):
        return item

    async def enqueue(_db, production_id, step_id):
        enqueued.append((production_id, step_id))

    monkeypatch.setattr(production_service, "_get_locked_production", get_locked)
    monkeypatch.setattr(production_service, "get_agent_production_or_404", get_detail)
    monkeypatch.setattr(production_service, "_enqueue_source_analysis", enqueue)

    await production_service.apply_agent_production_action(db, item.id, user, "resume")

    assert item.status == "running"
    assert step.status == "queued"
    assert step.extra["redispatch_requested"] is True
    assert enqueued == [(item.id, step.id)]


@pytest.mark.asyncio
async def test_cancel_skips_steps_that_have_not_started(monkeypatch) -> None:
    item = production(status="planning")
    user = SimpleNamespace(id=item.user_id)
    step = AgentStep(status="queued")
    db = FakeSession(results=[FakeResult(items=[step])])

    async def get_production(_db, _production_id, _user_id):
        return item

    monkeypatch.setattr(production_service, "_get_locked_production", get_production)
    monkeypatch.setattr(production_service, "get_agent_production_or_404", get_production)

    await production_service.apply_agent_production_action(db, item.id, user, "cancel")

    assert item.status == "cancelled"
    assert step.status == "skipped"
    event = next(added for added in db.added if isinstance(added, AgentEvent))
    assert event.event_type == "production.cancelled"


@pytest.mark.asyncio
async def test_delete_agent_project_cancels_active_production(monkeypatch) -> None:
    item = production(status="planning")
    user = SimpleNamespace(id=item.user_id)
    project = SimpleNamespace(
        id=item.project_id,
        user_id=item.user_id,
        project_kind="agent",
        is_enabled=True,
        updated_at=None,
    )
    step = AgentStep(status="queued")
    db = FakeSession(
        results=[
            FakeResult(scalar=project),
            FakeResult(items=[step]),
        ]
    )

    async def get_locked(_db, _production_id, _user_id):
        return item

    monkeypatch.setattr(production_service, "_get_locked_production", get_locked)

    result = await production_service.delete_agent_production(db, item.id, user)

    assert result == {
        "production_id": item.id,
        "status": "cancelled",
        "deleted": True,
    }
    assert item.status == "cancelled"
    assert item.lock_version == 1
    assert step.status == "skipped"
    assert project.is_enabled is False
    event = next(added for added in db.added if isinstance(added, AgentEvent))
    assert event.event_type == "production.deleted"
    assert event.payload["from_status"] == "planning"
    assert event.payload["to_status"] == "cancelled"
    assert db.commits == 1


@pytest.mark.asyncio
async def test_delete_completed_agent_project_preserves_final_status(monkeypatch) -> None:
    item = production(status="completed")
    user = SimpleNamespace(id=item.user_id)
    project = SimpleNamespace(
        id=item.project_id,
        user_id=item.user_id,
        project_kind="agent",
        is_enabled=True,
        updated_at=None,
    )
    db = FakeSession(results=[FakeResult(scalar=project)])

    async def get_locked(_db, _production_id, _user_id):
        return item

    monkeypatch.setattr(production_service, "_get_locked_production", get_locked)

    result = await production_service.delete_agent_production(db, item.id, user)

    assert result["status"] == "completed"
    assert result["deleted"] is True
    assert item.status == "completed"
    assert project.is_enabled is False
    assert db.commits == 1
