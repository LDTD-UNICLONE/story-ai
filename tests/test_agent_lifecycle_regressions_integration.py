import asyncio
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.exceptions import AppException
from app.core.timezone import beijing_datetime
from app.models.agent_production import AgentProduction, AgentStep
from app.models.agent_core_asset import AgentCoreAssetLock
from app.models.agent_review import AgentDelivery
from app.models.agent_story_bible import SeriesBibleVersion
from app.models.agent_storyboard_media import AgentStoryboardMediaRequest
from app.models.project_storyboard import ProjectStoryboard
from app.models.task_dispatch import TaskDispatchOutbox
from app.models.task_record import UserTaskRecord
from app.schemas.agent_review import AgentDeliveryCreateRequest, AgentEpisodeApproveRequest
from app.services.agent import reviews
from app.services.agent import storyboard_media
from app.services.generation import task_dispatch
from tests import test_agent_production_integration as agent_fixtures


agent_api = agent_fixtures.agent_api
pytestmark = agent_fixtures.pytestmark


async def test_first_start_persists_dispatch_and_repeated_start_does_not_duplicate(
    agent_api,
    monkeypatch,
):
    def unavailable(**kwargs):
        raise ConnectionError("test broker unavailable")

    monkeypatch.setattr(task_dispatch, "publish_task_message", unavailable)
    response = await agent_api.client.post(
        "/api/v1/agent-productions/from-text",
        json={
            "content": "第一集：雨夜相遇",
            "style_id": str(agent_api.style.id),
            "generation_ratio": "9:16",
            "video_resolution": "720p",
            "mode": "supervised",
        },
    )
    assert response.status_code == 200
    production_id = UUID(response.json()["data"]["production_id"])
    for _ in range(2):
        started = await agent_api.client.post(f"/api/v1/agent-productions/{production_id}/start")
        assert started.status_code == 200
        assert started.json()["data"]["status"] == "planning"
    step = (
        await agent_api.session.scalars(
            select(AgentStep).where(
                AgentStep.production_id == production_id,
                AgentStep.stage == "source_analysis",
            )
        )
    ).one()
    records = (await agent_api.session.scalars(select(TaskDispatchOutbox))).all()
    assert len(records) == 1
    assert records[0].task_name == "tasks.agent_source_analysis.run_source_analysis"
    assert records[0].queue == "story_ai_text"
    assert records[0].args == [str(production_id), str(step.id)]


async def _ready_delivery_scope(api):
    storyboard = ProjectStoryboard(
        id=uuid4(),
        project_id=api.project.id,
        chapter_id=api.chapter.id,
        user_id=api.owner.id,
        shot_number=1,
        title="雨夜",
        source_content="雨夜相遇",
        action="推门",
        video_prompt="雨夜推门",
        characters=[],
        props=[],
        is_enabled=True,
        extra={
            "video_generation_status": "success",
            "video_generation_result": "https://example.com/video.mp4",
        },
    )
    api.production.status = "running"
    api.session.add(storyboard)
    await api.session.commit()
    await reviews.approve_agent_episode(
        api.session,
        api.production.id,
        api.chapter.id,
        api.owner,
        AgentEpisodeApproveRequest(expected_lock_version=0, idempotency_key="approve-test"),
    )
    return storyboard


@pytest.mark.parametrize("conflicting", [False, True])
async def test_concurrent_delivery_requests_obey_idempotency(agent_api, monkeypatch, conflicting):
    await _ready_delivery_scope(agent_api)
    factory = async_sessionmaker(agent_api.session.bind, expire_on_commit=False)
    production_id, owner = agent_api.production.id, agent_api.owner
    await agent_api.session.commit()
    original = reviews._lock_production
    arrived = 0
    both_arrived = asyncio.Event()
    seen_sessions = set()

    async def synchronized_lock(db, *args):
        nonlocal arrived
        if id(db) not in seen_sessions:
            seen_sessions.add(id(db))
            arrived += 1
            if arrived == 2:
                both_arrived.set()
            await asyncio.wait_for(both_arrived.wait(), timeout=5)
        await original(db, *args)

    monkeypatch.setattr(reviews, "_lock_production", synchronized_lock)
    monkeypatch.setattr(task_dispatch, "publish_task_message", lambda **kwargs: None)

    async def submit(delivery_type):
        async with factory() as db:
            try:
                result = await reviews.create_agent_delivery(
                    db,
                    production_id,
                    owner,
                    AgentDeliveryCreateRequest(
                        delivery_type=delivery_type,
                        idempotency_key="concurrent-delivery",
                    ),
                )
                return result.id
            except AppException as exc:
                return exc.code

    results = await asyncio.wait_for(
        asyncio.gather(
            submit("manifest"),
            submit("merged_video" if conflicting else "manifest"),
        ),
        timeout=10,
    )
    if conflicting:
        assert sum(isinstance(item, UUID) for item in results) == 1
        assert 40976 in results
    else:
        assert results[0] == results[1]
    assert await agent_api.session.scalar(select(func.count()).select_from(AgentDelivery)) == 1


async def _pending_delivery(api):
    delivery = AgentDelivery(
        id=uuid4(),
        production_id=api.production.id,
        project_id=api.project.id,
        user_id=api.owner.id,
        delivery_type="merged_video",
        status="pending",
        idempotency_key="lease-test",
        attempt_count=0,
        extra={},
        manifest={"episodes": [{"shots": [{"video_url": "https://example.com/clip.mp4"}]}]},
    )
    api.session.add(delivery)
    await api.session.commit()
    return delivery.id


async def test_expired_delivery_worker_cannot_overwrite_new_claim(agent_api, monkeypatch):
    delivery_id = await _pending_delivery(agent_api)
    factory = async_sessionmaker(agent_api.session.bind, expire_on_commit=False)
    old_token, _ = await reviews.claim_agent_delivery(agent_api.session, delivery_id)
    started, finish = asyncio.Event(), asyncio.Event()

    async def build(urls, delivery):
        started.set()
        await asyncio.wait_for(finish.wait(), timeout=5)
        return "https://example.com/stale.mp4"

    monkeypatch.setattr(reviews, "_merge_and_upload_videos", build)
    async with factory() as old_db, factory() as new_db:
        task = asyncio.create_task(reviews.run_agent_delivery(old_db, delivery_id, old_token))
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            delivery = await new_db.get(AgentDelivery, delivery_id)
            delivery.lease_expires_at = beijing_datetime() - timedelta(seconds=1)
            await new_db.commit()
            new_token, _ = await reviews.claim_agent_delivery(new_db, delivery_id)
            assert new_token != old_token
            finish.set()
            await task
            await new_db.refresh(delivery)
            assert delivery.status == "running"
            assert delivery.lease_token == new_token
            assert delivery.output_url is None
        finally:
            finish.set()
            await task


async def test_delivery_build_does_not_hold_database_transaction(agent_api, monkeypatch):
    delivery_id = await _pending_delivery(agent_api)
    token, _ = await reviews.claim_agent_delivery(agent_api.session, delivery_id)

    async def build(urls, delivery):
        assert not agent_api.session.in_transaction()
        assert delivery.id == delivery_id
        return "https://example.com/result.mp4"

    monkeypatch.setattr(reviews, "_merge_and_upload_videos", build)
    await reviews.run_agent_delivery(agent_api.session, delivery_id, token)
    delivery = await agent_api.session.get(AgentDelivery, delivery_id)
    assert delivery.status == "completed"


async def test_stale_delivery_failure_does_not_overwrite_new_claim(agent_api):
    delivery_id = await _pending_delivery(agent_api)
    old_token, _ = await reviews.claim_agent_delivery(agent_api.session, delivery_id)
    factory = async_sessionmaker(agent_api.session.bind, expire_on_commit=False)
    async with factory() as old_db, factory() as new_db:
        stale = await old_db.get(AgentDelivery, delivery_id)
        delivery = await new_db.get(AgentDelivery, delivery_id)
        delivery.lease_expires_at = beijing_datetime() - timedelta(seconds=1)
        await new_db.commit()
        new_token, _ = await reviews.claim_agent_delivery(new_db, delivery_id)
        assert stale.lease_token == old_token
        await reviews.fail_agent_delivery(old_db, delivery_id, "旧任务失败", lease_token=old_token)
        await new_db.refresh(delivery)
        assert delivery.status == "running"
        assert delivery.lease_token == new_token


async def _video_scope(api):
    storyboard = await _ready_delivery_scope(api)
    storyboard.extra = {"video_generation_status": "not_started"}
    step_id = await api.session.scalar(
        select(AgentStep.id).where(
            AgentStep.production_id == api.production.id,
        )
    )
    bible = SeriesBibleVersion(
        id=uuid4(),
        project_id=api.project.id,
        production_id=api.production.id,
        step_id=step_id,
        version=1,
        status="confirmed",
        content={},
        created_by=api.owner.id,
    )
    api.session.add(bible)
    await api.session.flush()
    api.session.add(
        AgentCoreAssetLock(
            project_id=api.project.id,
            production_id=api.production.id,
            bible_version_id=bible.id,
            step_id=step_id,
            version=1,
            status="active",
            assets=[],
            impact={},
            idempotency_key="video-core-lock",
            created_by=api.owner.id,
        )
    )
    await api.session.commit()
    return storyboard


@pytest.mark.parametrize(
    "status,stage,code",
    [
        ("paused", "episode_videos", 40952),
        ("cancelled", "episode_videos", 40951),
        ("completed", "completed", 40951),
        ("running", "core_asset_change_review", 40950),
    ],
)
@pytest.mark.parametrize("single", [False, True])
async def test_video_endpoints_do_not_bypass_production_state(
    agent_api,
    monkeypatch,
    status,
    stage,
    code,
    single,
):
    storyboard = await _video_scope(agent_api)
    agent_api.production.status = status
    agent_api.production.current_stage = stage
    await agent_api.session.commit()
    calls = []

    async def submit(*args, **kwargs):
        calls.append(True)
        raise AppException("test submission reached", code=50000, status_code=500)

    monkeypatch.setattr(storyboard_media, "_submit_storyboard_video", submit)
    prefix = f"/api/v1/agent-productions/{agent_api.production.id}/episodes/{agent_api.chapter.id}"
    if single:
        prefix += f"/storyboards/{storyboard.id}"
    payload = {
        "expected_core_asset_lock_version": 1,
        "expected_episode_revision": 1,
        "idempotency_key": "blocked-video-request",
        "video_model_id": str(agent_api.video_model.id),
        "video_resolution": "720p",
    }
    if single:
        payload.update(
            expected_storyboard_revision=1, expected_video_config_version=0, duration_seconds=5
        )
    response = await agent_api.client.post(f"{prefix}/video-generations", json=payload)
    assert response.status_code == 409, response.text
    assert response.json()["code"] == code
    assert calls == []
    assert (
        await agent_api.session.scalar(
            select(func.count()).select_from(AgentStoryboardMediaRequest)
        )
        == 0
    )


async def test_episode_submission_stops_after_pause_and_replay_keeps_existing_result(
    agent_api,
    monkeypatch,
):
    await _video_scope(agent_api)
    second = ProjectStoryboard(
        project_id=agent_api.project.id,
        chapter_id=agent_api.chapter.id,
        user_id=agent_api.owner.id,
        shot_number=2,
        title="第二镜",
        source_content="进入旧宅",
        action="推门",
        video_prompt="雨夜推门",
        characters=[],
        props=[],
        is_enabled=True,
        extra={"video_generation_status": "not_started"},
    )
    agent_api.session.add(second)
    await agent_api.session.commit()
    factory = async_sessionmaker(agent_api.session.bind, expire_on_commit=False)
    calls = []

    async def submit(db, context, storyboard, user, *args, **kwargs):
        calls.append(storyboard.id)
        record = UserTaskRecord(
            user_id=user.id,
            business_type="project",
            business_id=context.project.id,
            generation_type="storyboard_video",
            status="pending",
            title="测试提交",
            prompt="测试",
            points_cost=0,
            extra={},
        )
        db.add(record)
        await db.commit()
        # Another request pauses the production after the first submission releases its lock.
        async with factory() as other:
            production = await other.get(AgentProduction, context.production.id)
            production.status = "paused"
            await other.commit()
        return record, 0

    monkeypatch.setattr(storyboard_media, "_submit_storyboard_video", submit)
    path = (
        f"/api/v1/agent-productions/{agent_api.production.id}"
        f"/episodes/{agent_api.chapter.id}/video-generations"
    )
    payload = {
        "expected_core_asset_lock_version": 1,
        "expected_episode_revision": 1,
        "idempotency_key": "pause-between-submissions",
        "video_model_id": str(agent_api.video_model.id),
        "video_resolution": "720p",
    }
    response = await agent_api.client.post(path, json=payload)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["submitted_count"] == 1
    assert data["failed_count"] == 1
    assert data["items"][1]["error_code"] == 40952
    assert len(calls) == 1
    replay = await agent_api.client.post(path, json=payload)
    assert replay.status_code == 200, replay.text
    assert replay.json()["data"]["request_id"] == data["request_id"]
    assert replay.json()["data"]["idempotent_replay"] is True
    assert len(calls) == 1


async def test_delivery_worker_rolls_back_database_error_before_recording_failure(
    agent_api,
    monkeypatch,
):
    from app.tasks import agent_delivery

    delivery_id = await _pending_delivery(agent_api)
    factory = async_sessionmaker(agent_api.session.bind, expire_on_commit=False)
    monkeypatch.setattr(agent_delivery, "WorkerSessionLocal", factory)

    async def fail_in_transaction(db, *args):
        await db.execute(text("SELECT 1 / 0"))

    monkeypatch.setattr(agent_delivery, "run_agent_delivery", fail_in_transaction)
    assert await agent_delivery._build_agent_delivery(delivery_id) == 0
    delivery = await agent_api.session.get(AgentDelivery, delivery_id)
    assert delivery.status == "failed"
    assert delivery.lease_token is None
    assert delivery.error_summary == "成片交付生成失败"
