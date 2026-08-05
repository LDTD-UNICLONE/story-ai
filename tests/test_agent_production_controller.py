from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services import agent_production_controller as controller_service
from app.services.agent_task_context import build_agent_task_context
from app.tasks import agent_delivery as delivery_tasks
from app.tasks import agent_production_controller as controller_tasks
from app.worker import celery_app


class FakeResult:
    def __init__(self, row):
        self.row = row

    def one_or_none(self):
        return self.row


class FakeSession:
    def __init__(self, row):
        self.row = row
        self.refreshed = []

    async def execute(self, _statement):
        return FakeResult(self.row)

    async def refresh(self, value):
        self.refreshed.append(value)


def test_agent_controller_tasks_are_registered() -> None:
    assert (
        controller_tasks.advance_agent_batch_production_task.name
        == "tasks.agent_production_controller.advance_agent_batch_production"
    )
    assert (
        controller_tasks.enqueue_agent_batch_productions.name
        == "tasks.agent_production_controller.enqueue_agent_batch_productions"
    )
    assert "enqueue-agent-batch-productions" in celery_app.conf.beat_schedule
    assert delivery_tasks.build_agent_delivery.name == "tasks.agent_delivery.build_agent_delivery"
    assert celery_app.conf.task_routes[delivery_tasks.build_agent_delivery.name]["queue"] == (
        "story_ai_delivery"
    )


def test_agent_task_context_has_stable_ownership_fields() -> None:
    production_id = uuid4()
    step_id = uuid4()
    scope_id = uuid4()

    context = build_agent_task_context(
        production_id=production_id,
        step_id=step_id,
        stage="batch_videos",
        scope_type="storyboard",
        scope_id=scope_id,
        attempt_number=2,
    )

    assert context == {
        "agent_production_id": str(production_id),
        "agent_step_id": str(step_id),
        "agent_stage": "batch_videos",
        "agent_scope_type": "storyboard",
        "agent_scope_id": str(scope_id),
        "agent_attempt_number": 2,
    }


@pytest.mark.asyncio
async def test_controller_dispatches_when_queue_capacity_is_available(monkeypatch) -> None:
    production = SimpleNamespace(
        id=uuid4(),
        user_id=uuid4(),
        lock_version=7,
        mode="automatic",
    )
    user = SimpleNamespace(id=production.user_id)
    db = FakeSession((production, user))
    dispatched = []

    async def get_status(_db, _production_id, _user_id):
        return {
            "core_asset_lock_version": 3,
            "can_dispatch": True,
            "paused": False,
            "is_complete": False,
            "active_task_count": 0,
            "recommended_batch_size": 4,
        }

    async def dispatch(_db, production_id, current_user, payload, *, dispatch_source):
        dispatched.append((production_id, current_user.id, payload, dispatch_source))
        return {}

    monkeypatch.setattr(controller_service, "get_batch_production", get_status)
    monkeypatch.setattr(controller_service, "dispatch_batch_production", dispatch)

    advanced = await controller_service.advance_agent_batch_production(db, production.id)

    assert advanced is True
    assert dispatched[0][0] == production.id
    assert dispatched[0][2].expected_core_asset_lock_version == 3
    assert dispatched[0][2].max_tasks == 4
    assert dispatched[0][3] == "system"


@pytest.mark.asyncio
async def test_controller_waits_when_queue_has_no_capacity(monkeypatch) -> None:
    production = SimpleNamespace(
        id=uuid4(),
        user_id=uuid4(),
        lock_version=2,
        mode="automatic",
    )
    user = SimpleNamespace(id=production.user_id)
    db = FakeSession((production, user))

    async def get_status(_db, _production_id, _user_id):
        return {
            "can_dispatch": True,
            "paused": False,
            "is_complete": False,
            "active_task_count": 1,
            "recommended_batch_size": 0,
        }

    monkeypatch.setattr(controller_service, "get_batch_production", get_status)

    advanced = await controller_service.advance_agent_batch_production(db, production.id)

    assert advanced is False
    assert db.refreshed == []


@pytest.mark.asyncio
async def test_controller_never_dispatches_supervised_production(monkeypatch) -> None:
    production = SimpleNamespace(
        id=uuid4(),
        user_id=uuid4(),
        lock_version=2,
        mode="supervised",
        current_stage="batch_production",
        production_spec={"workflow_version": 1},
    )
    user = SimpleNamespace(id=production.user_id)
    db = FakeSession((production, user))

    async def unexpected_status(*_args, **_kwargs):
        raise AssertionError("审核模式不应进入自动调度状态查询")

    monkeypatch.setattr(controller_service, "get_batch_production", unexpected_status)

    assert await controller_service.advance_agent_batch_production(db, production.id) is False
    assert db.refreshed == []


@pytest.mark.asyncio
async def test_controller_recovers_new_supervised_storyboard_generation(
    monkeypatch,
) -> None:
    production = SimpleNamespace(
        id=uuid4(),
        user_id=uuid4(),
        lock_version=7,
        mode="supervised",
        current_stage="batch_production",
        production_spec={"workflow_version": 2},
    )
    user = SimpleNamespace(id=production.user_id)
    db = FakeSession((production, user))
    dispatched = []

    async def get_status(_db, _production_id, _user_id):
        return {
            "core_asset_lock_version": 3,
            "can_dispatch": True,
            "paused": False,
            "is_complete": False,
            "recommended_batch_size": 2,
        }

    async def dispatch(_db, production_id, current_user, payload, *, dispatch_source):
        dispatched.append((production_id, current_user.id, payload, dispatch_source))
        return {}

    monkeypatch.setattr(controller_service, "get_batch_production", get_status)
    monkeypatch.setattr(controller_service, "dispatch_batch_production", dispatch)

    advanced = await controller_service.advance_agent_batch_production(db, production.id)

    assert advanced is True
    assert dispatched[0][0] == production.id
    assert dispatched[0][2].max_tasks == 2
    assert dispatched[0][3] == "system"
