from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.core.exceptions import AppException
from app.main import app
from app.models.agent_production import AgentStep
from app.schemas.agent_batch_production import (
    AgentBatchDispatchRequest,
    AgentVideoModelSelectionRequest,
)
from app.services.agent import batch_productions as batch_service
from app.services.agent.productions import _agent_step_task_record_ids
from app.services.agent.workflow import agent_pilot_episode_count


def test_agent_batch_routes_are_registered() -> None:
    routes = {
        (path, method.upper())
        for path, methods in app.openapi()["paths"].items()
        for method in methods
    }
    prefix = "/api/v1/agent-productions/{production_id}/batch"
    assert (prefix, "GET") in routes
    assert (f"{prefix}/dispatch", "POST") in routes
    assert (f"{prefix}/video-model", "POST") in routes


def test_batch_dispatch_normalizes_key_and_limits_batch_size() -> None:
    payload = AgentBatchDispatchRequest(
        expected_core_asset_lock_version=2,
        idempotency_key="  batch-dispatch-v2  ",
        max_tasks=5,
    )
    assert payload.idempotency_key == "batch-dispatch-v2"

    with pytest.raises(ValidationError):
        AgentBatchDispatchRequest(
            expected_core_asset_lock_version=0,
            idempotency_key="short",
            max_tasks=51,
        )

    with pytest.raises(ValidationError):
        AgentVideoModelSelectionRequest(
            expected_core_asset_lock_version=2,
            video_model_id=uuid4(),
        )


def test_cancel_collects_all_batch_leaf_task_ids() -> None:
    task_ids = [uuid4(), uuid4(), uuid4()]
    step = AgentStep(
        production_id=uuid4(),
        stage="batch_production",
        scope_type="production",
        scope_id=uuid4(),
        status="running",
        input_version=1,
        progress_current=0,
        progress_total=1,
        attempt_count=1,
        extra={
            "storyboard_task_ids": {"chapter": str(task_ids[0])},
            "image_task_ids": {"shot": str(task_ids[1])},
            "video_task_ids": {"shot": str(task_ids[2])},
        },
    )
    assert _agent_step_task_record_ids(step) == task_ids


def test_new_workflow_batches_all_episodes_without_pilot() -> None:
    item = SimpleNamespace(
        production_spec={
            "workflow_version": 2,
            "pilot_episode_count": 0,
        }
    )
    legacy = SimpleNamespace(production_spec={})

    assert agent_pilot_episode_count(item) == 0
    assert agent_pilot_episode_count(legacy) == 1


@pytest.mark.asyncio
async def test_incremental_dispatch_extends_persisted_storyboard_scope(monkeypatch) -> None:
    first_chapter_id = uuid4()
    second_chapter_id = uuid4()
    production = SimpleNamespace(
        id=uuid4(),
        status="running",
        current_stage="batch_storyboards",
        error_summary=None,
        production_spec={"workflow_version": 2},
        lock_version=1,
    )
    step = SimpleNamespace(
        id=uuid4(),
        status="running",
        finished_at=None,
        extra={
            "phase": "storyboards",
            "dispatch_idempotency_keys": [],
            "storyboard_scope_ids": [str(first_chapter_id)],
        },
    )
    context = SimpleNamespace(
        production=production,
        step=step,
        core_lock=SimpleNamespace(version=1),
        chapters=[],
        storyboards=[],
    )

    class Db:
        def add(self, _item):
            return None

        async def commit(self):
            return None

    async def get_context(*_args, **_kwargs):
        return context

    async def no_op(*_args, **_kwargs):
        return None

    async def no_slots(*_args, **_kwargs):
        return 0

    async def phase_model(*_args, **_kwargs):
        return SimpleNamespace()

    async def status(*_args, **_kwargs):
        return {"phase": "storyboards"}

    monkeypatch.setattr(batch_service, "_get_context", get_context)
    monkeypatch.setattr(batch_service, "_ensure_pilot_completed", no_op)
    monkeypatch.setattr(batch_service, "_reconcile", no_op)
    monkeypatch.setattr(batch_service, "_available_task_slots", no_slots)
    monkeypatch.setattr(batch_service, "_phase_model", phase_model)
    monkeypatch.setattr(batch_service, "_ensure_budget", no_op)
    monkeypatch.setattr(batch_service, "_refresh_status", status)

    await batch_service.dispatch_batch_production(
        Db(),
        production.id,
        SimpleNamespace(id=uuid4()),
        AgentBatchDispatchRequest(
            expected_core_asset_lock_version=1,
            idempotency_key="incremental-storyboards-v2",
            max_tasks=1,
        ),
        scope_ids=[second_chapter_id],
        replace_storyboard_scope=True,
    )

    assert set(step.extra["storyboard_scope_ids"]) == {
        str(first_chapter_id),
        str(second_chapter_id),
    }


@pytest.mark.asyncio
async def test_failed_storyboard_episode_waits_for_other_episode_work(monkeypatch) -> None:
    failed = SimpleNamespace(
        id=uuid4(),
        extra={"episode_number": 1, "storyboard_analysis_status": "failed"},
    )
    waiting = SimpleNamespace(
        id=uuid4(),
        extra={"episode_number": 2, "storyboard_analysis_status": "not_started"},
    )
    step = SimpleNamespace(
        status="running",
        progress_current=0,
        extra={"phase": "storyboards", "storyboard_attempts": {str(failed.id): 1}},
    )
    production = SimpleNamespace(
        status="running",
        current_stage="batch_storyboards",
        error_summary=None,
        production_spec={"workflow_version": 2},
    )
    context = SimpleNamespace(
        step=step,
        production=production,
        chapters=[failed, waiting],
        storyboards=[],
    )

    async def account_refunds(*_args, **_kwargs):
        return None

    monkeypatch.setattr(batch_service, "_account_refunded_tasks", account_refunds)

    await batch_service._reconcile(SimpleNamespace(), context)

    assert step.extra["phase"] == "storyboards"
    assert production.status == "running"

    waiting.extra["storyboard_analysis_status"] = "success"
    await batch_service._reconcile(SimpleNamespace(), context)

    assert step.extra["phase"] == "exceptions"
    assert production.status == "partially_failed"
    assert production.error_summary == "有 1 集分镜分析失败，请处理失败集"


@pytest.mark.asyncio
async def test_legacy_video_model_is_selected_only_in_video_phase(monkeypatch) -> None:
    production = SimpleNamespace(
        id=uuid4(),
        status="running",
        production_spec={"workflow_version": 1},
        lock_version=2,
    )
    step = SimpleNamespace(
        id=uuid4(),
        extra={
            "phase": "videos",
            "video_attempts": {},
            "video_task_ids": {},
        },
    )
    context = SimpleNamespace(
        production=production,
        step=step,
        core_lock=SimpleNamespace(version=3),
    )
    video_model = SimpleNamespace(
        id=uuid4(),
        model_id="video-model",
        model_type="video",
        is_enabled=True,
    )
    user = SimpleNamespace(id=uuid4())

    class Db:
        def __init__(self):
            self.added = []
            self.commits = 0

        def add(self, item):
            self.added.append(item)

        async def commit(self):
            self.commits += 1

    db = Db()

    async def get_context(*_args, **_kwargs):
        return context

    async def get_video_model(_db, model_id):
        assert model_id == video_model.id
        return video_model

    async def status(_db, _context):
        return {"selected_video_model_id": video_model.id}

    monkeypatch.setattr(batch_service, "_get_context", get_context)
    monkeypatch.setattr(batch_service, "get_agent_video_model", get_video_model)
    monkeypatch.setattr(batch_service, "_status", status)

    result = await batch_service.select_batch_video_model(
        db,
        production.id,
        user,
        AgentVideoModelSelectionRequest(
            expected_core_asset_lock_version=3,
            video_model_id=video_model.id,
            video_resolution="1080p",
        ),
    )

    assert result["selected_video_model_id"] == video_model.id
    assert production.production_spec["video_model_id"] == str(video_model.id)
    assert production.production_spec["video_resolution"] == "1080p"
    assert production.lock_version == 3
    assert db.commits == 1


@pytest.mark.asyncio
async def test_new_workflow_rejects_global_video_model_selection(monkeypatch) -> None:
    production = SimpleNamespace(
        id=uuid4(),
        status="running",
        production_spec={"workflow_version": 2},
    )
    context = SimpleNamespace(production=production)

    async def get_context(*_args, **_kwargs):
        return context

    monkeypatch.setattr(batch_service, "_get_context", get_context)

    with pytest.raises(AppException) as exc_info:
        await batch_service.select_batch_video_model(
            SimpleNamespace(),
            production.id,
            SimpleNamespace(id=uuid4()),
            AgentVideoModelSelectionRequest(
                expected_core_asset_lock_version=1,
                video_model_id=uuid4(),
                video_resolution="720p",
            ),
        )

    assert exc_info.value.code == 40986
    assert "按分集" in exc_info.value.message


@pytest.mark.asyncio
async def test_new_workflow_leaves_global_media_pipeline_after_storyboards(
    monkeypatch,
) -> None:
    production = SimpleNamespace(
        production_spec={"workflow_version": 2},
        current_stage="batch_images",
    )
    step = SimpleNamespace(extra={"phase": "images"}, progress_current=0)
    context = SimpleNamespace(
        production=production,
        step=step,
        chapters=[],
        storyboards=[],
    )

    async def account_refunds(*_args, **_kwargs):
        return None

    monkeypatch.setattr(batch_service, "_account_refunded_tasks", account_refunds)

    await batch_service._reconcile(SimpleNamespace(), context)

    assert step.extra["phase"] == "episode_videos"
    assert production.current_stage == "episode_videos"
