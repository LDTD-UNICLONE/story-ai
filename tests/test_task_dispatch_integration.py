import asyncio
import os
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app.core.timezone import beijing_datetime
from app.models.ai_model import AiModel
from app.models.points import UserPointsTransaction
from app.models.project_chapter import ProjectChapter
from app.models.task_dispatch import TaskDispatchOutbox
from app.models.task_record import UserTaskRecord
from app.models.user import User
from app.services.billing.points import consume_user_points
from app.services.generation import task_dispatch, task_records
from app.services.generation.runner import ModelRunResult
from app.services.generation.task_execution import TaskExecutionDeferred
from test_task_lifecycle_integration import _seed_task, lifecycle_db as lifecycle_db


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_DB_INTEGRATION_TESTS") != "1", reason="requires test PostgreSQL"
    ),
]


async def enqueue(db, message_id=None):
    return await task_dispatch.enqueue_task_dispatch(
        db,
        task_name="tasks.project_chapter.run_project_chapter_processing",
        args=(str(uuid4()), str(uuid4())),
        queue="story_ai_text",
        message_id=message_id,
    )


async def test_uncommitted_intent_is_invisible_and_rollback_discards_it(lifecycle_db, monkeypatch):
    published = []
    monkeypatch.setattr(task_dispatch, "publish_task_message", lambda **kw: published.append(kw))
    async with lifecycle_db() as writer, lifecycle_db() as reader:
        message_id = await enqueue(writer)
        assert await task_dispatch.dispatch_pending_tasks(reader) == 0
        assert published == []
        await writer.rollback()
        assert await reader.get(TaskDispatchOutbox, message_id) is None
        await enqueue(writer, message_id)
        await writer.commit()
        # No immediate publishing call: simulate exit immediately after the business commit.
        assert await task_dispatch.dispatch_pending_tasks(reader) == 1
        assert published[0]["message_id"] == str(message_id)
        assert await reader.get(TaskDispatchOutbox, message_id) is None


async def test_submission_rollback_includes_charge_expiration_and_outbox(lifecycle_db):
    seed = await _seed_task(lifecycle_db, "chapter_text_process")
    async with lifecycle_db() as db:
        old = await db.get(UserTaskRecord, seed.task_id)
        old.provider_task_id = None
        old.created_at = beijing_datetime() - timedelta(days=1)
        await db.commit()
        # Creation expires an older task while this new charge is still uncommitted.
        charge = await consume_user_points(
            db,
            user_id=seed.user_id,
            amount=5,
            remark="new task",
            auto_commit=False,
        )
        new = await task_records.create_user_task_record(
            db,
            user_id=seed.user_id,
            business_type="project",
            generation_type="chapter_text_process",
            status="pending",
            title="New task",
            prompt="test",
            points_cost=5,
            points_transaction_id=charge.id,
        )
        await db.flush()
        new_id = new.id
        await enqueue(db, new_id)
        await db.rollback()
    async with lifecycle_db() as db:
        assert await db.get(UserTaskRecord, new_id) is None
        assert await db.get(TaskDispatchOutbox, new_id) is None
        assert (await db.get(User, seed.user_id)).points_balance == 90
        assert (await db.get(UserTaskRecord, seed.task_id)).status == "running"
        assert await db.scalar(select(func.count()).select_from(UserPointsTransaction)) == 0


async def test_broker_failure_defers_delivery_and_recovers_same_message(lifecycle_db, monkeypatch):
    def unavailable(**kw):
        raise ConnectionError("broker offline")

    monkeypatch.setattr(task_dispatch, "publish_task_message", unavailable)
    async with lifecycle_db() as db:
        message_id = await enqueue(db)
        await db.commit()
        assert await task_dispatch.dispatch_pending_tasks(db) == 0
        row = await db.get(TaskDispatchOutbox, message_id)
        assert row.attempt_count == 1
        assert row.last_error == "broker offline"
        assert row.next_attempt_at > beijing_datetime()
        assert await task_dispatch.dispatch_pending_tasks(db) == 0
        assert row.attempt_count == 1
        row.next_attempt_at = beijing_datetime() - timedelta(seconds=1)
        await db.commit()
        published = []
        monkeypatch.setattr(
            task_dispatch, "publish_task_message", lambda **kw: published.append(kw)
        )
        assert await task_dispatch.dispatch_pending_tasks(db) == 1
        assert published == [
            {
                "task_name": row.task_name,
                "args": row.args,
                "queue": "story_ai_text",
                "message_id": str(message_id),
            }
        ]
        assert await task_dispatch.dispatch_pending_tasks(db) == 0


async def test_concurrent_sweeps_skip_claimed_messages(lifecycle_db, monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()
    published = []

    async def blocked_publish(_function, **kwargs):
        published.append(kwargs)
        entered.set()
        await asyncio.wait_for(release.wait(), timeout=5)

    monkeypatch.setattr(task_dispatch, "run_in_threadpool", blocked_publish)
    async with lifecycle_db() as db:
        await enqueue(db)
        await db.commit()
    async with lifecycle_db() as first, lifecycle_db() as second:
        sweep = asyncio.create_task(task_dispatch.dispatch_pending_tasks(first))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            assert (
                await asyncio.wait_for(task_dispatch.dispatch_pending_tasks(second), timeout=2) == 0
            )
        finally:
            release.set()
            assert await sweep == 1
    assert len(published) == 1


async def test_publish_commit_failure_redelivers_without_duplicate_generation(
    lifecycle_db, monkeypatch
):
    from app.tasks import project_chapter

    seed = await _seed_task(lifecycle_db, "chapter_text_process")
    async with lifecycle_db() as db:
        record = await db.get(UserTaskRecord, seed.task_id)
        record.status = "pending"
        record.provider_task_id = None
        model = await db.get(AiModel, seed.model_id)
        model.model_type = "text"
        model.vendor = "integration"
        await task_dispatch.enqueue_task_dispatch(
            db,
            task_name=project_chapter.run_project_chapter_processing.name,
            args=(str(seed.task_id), str(seed.chapter_id)),
            queue="story_ai_text",
            message_id=seed.task_id,
        )
        await db.commit()
    published = []
    monkeypatch.setattr(task_dispatch, "publish_task_message", lambda **kw: published.append(kw))
    async with lifecycle_db() as db:

        async def lost_commit():
            raise ConnectionError("connection lost after broker accepted message")

        with monkeypatch.context() as patch:
            patch.setattr(db, "commit", lost_commit)
            with pytest.raises(ConnectionError):
                await task_dispatch.dispatch_pending_tasks(db)
        await db.rollback()
        assert await task_dispatch.dispatch_pending_tasks(db) == 1
    assert len(published) == 2
    assert published[0] == published[1]

    entered = asyncio.Event()
    release = asyncio.Event()
    model_calls = []

    async def generate(*args, **kwargs):
        model_calls.append(kwargs)
        entered.set()
        await asyncio.wait_for(release.wait(), timeout=5)
        return ModelRunResult(content="Generated once", extra={})

    monkeypatch.setattr(project_chapter, "WorkerSessionLocal", lifecycle_db)
    monkeypatch.setattr(project_chapter, "run_model", generate)
    task_args = [UUID(arg) for arg in published[0]["args"]]
    first = asyncio.create_task(project_chapter._execute_processing(*task_args))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        with pytest.raises(TaskExecutionDeferred):
            await project_chapter._execute_processing(*task_args)
    finally:
        release.set()
        await first
    async with lifecycle_db() as db:
        balance = (await db.get(User, seed.user_id)).points_balance
        transactions = await db.scalar(select(func.count()).select_from(UserPointsTransaction))
    await project_chapter._execute_processing(*task_args)
    async with lifecycle_db() as db:
        assert len(model_calls) == 1
        assert (await db.get(UserTaskRecord, seed.task_id)).status == "success"
        assert (await db.get(ProjectChapter, seed.chapter_id)).processed_content == "Generated once"
        assert (await db.get(User, seed.user_id)).points_balance == balance
        assert (
            await db.scalar(select(func.count()).select_from(UserPointsTransaction)) == transactions
        )


async def test_outbox_failure_does_not_expire_committed_response_objects(lifecycle_db, monkeypatch):
    seed = await _seed_task(lifecycle_db)
    async with lifecycle_db() as db:
        user = await db.get(User, seed.user_id)
        message_id = await enqueue(db)
        await db.commit()

        async def database_unavailable(*args, **kwargs):
            raise ConnectionError("temporary database outage")

        monkeypatch.setattr(task_dispatch, "dispatch_pending_tasks", database_unavailable)
        await task_dispatch.dispatch_tasks_best_effort(db, [message_id])
        assert user.id == seed.user_id
        assert user.points_balance == 90
        assert await db.get(TaskDispatchOutbox, message_id) is not None


async def test_agent_result_and_next_stage_intent_commit_together(lifecycle_db, monkeypatch):
    from app.models.agent_production import AgentProduction, AgentStep, ProjectSourceDocument
    from app.services.agent.productions import apply_agent_production_action
    from app.services.agent.source_analysis import advance_source_analysis
    from app.tasks import agent_source_analysis

    seed = await _seed_task(lifecycle_db, "chapter_text_process")
    async with lifecycle_db() as db:
        model = await db.get(AiModel, seed.model_id)
        model.model_type = "text"
        model.vendor = "integration"
        source = ProjectSourceDocument(
            id=uuid4(),
            project_id=seed.project_id,
            user_id=seed.user_id,
            source_type="text",
            content="Rainy night",
            content_hash="a" * 64,
            character_count=11,
            version=1,
        )
        db.add(source)
        await db.flush()
        production = AgentProduction(
            id=uuid4(),
            project_id=seed.project_id,
            user_id=seed.user_id,
            source_document_id=source.id,
            status="paused",
            current_stage="source_analysis",
            production_spec={"text_model_id": str(seed.model_id)},
        )
        db.add(production)
        await db.flush()
        step = AgentStep(
            id=uuid4(),
            production_id=production.id,
            stage="source_analysis",
            scope_type="production",
            scope_id=production.id,
            status="queued",
            input_version=1,
        )
        db.add(step)
        await db.commit()
        production_id, step_id = production.id, step.id

        def unavailable(**kwargs):
            raise ConnectionError("broker unavailable")

        monkeypatch.setattr(task_dispatch, "publish_task_message", unavailable)
        await apply_agent_production_action(
            db,
            production_id,
            await db.get(User, seed.user_id),
            "resume",
        )
        initial_intent = await db.scalar(select(TaskDispatchOutbox))
        assert initial_intent.task_name == "tasks.agent_source_analysis.run_source_analysis"
        assert initial_intent.args == [str(production_id), str(step_id)]
        initial_intent.next_attempt_at = beijing_datetime() - timedelta(seconds=1)
        await db.commit()
        published = []
        monkeypatch.setattr(
            task_dispatch, "publish_task_message", lambda **kw: published.append(kw)
        )
        assert await task_dispatch.dispatch_pending_tasks(db) == 1
        first = await advance_source_analysis(db, production_id, step_id)
        assert len(first.task_record_ids) == 1
        task_id = first.task_record_ids[0]
        # A coordinator exit after creating its batch still leaves a durable text-task intent.
        assert (await db.get(TaskDispatchOutbox, task_id)).args == [str(task_id)]
        assert await task_dispatch.dispatch_pending_tasks(db) == 1

    model_calls = []

    async def generate(*args, **kwargs):
        model_calls.append(kwargs)
        return ModelRunResult(
            content='{"summary": "Rainy night", "characters": [], "scenes": [], "props": [], "character_variants": [], "scene_variants": [], "prop_variants": []}',
            extra={},
        )

    async def prompt(*args):
        return "Analyze the source"

    monkeypatch.setattr(agent_source_analysis, "WorkerSessionLocal", lifecycle_db)
    monkeypatch.setattr(agent_source_analysis, "run_model", generate)
    monkeypatch.setattr(agent_source_analysis, "build_agent_text_prompt", prompt)
    monkeypatch.setattr(task_dispatch, "publish_task_message", unavailable)
    assert await agent_source_analysis._execute_agent_text_task(task_id) == (production_id, step_id)
    await agent_source_analysis._execute_agent_text_task(task_id)
    async with lifecycle_db() as db:
        assert len(model_calls) == 1
        assert (await db.get(UserTaskRecord, task_id)).status == "success"
        next_intent = await db.scalar(select(TaskDispatchOutbox))
        assert next_intent.task_name == "tasks.agent_source_analysis.run_source_analysis"
        assert next_intent.args == [str(production_id), str(step_id)]
        next_intent.next_attempt_at = beijing_datetime() - timedelta(seconds=1)
        await db.commit()
        monkeypatch.setattr(
            task_dispatch, "publish_task_message", lambda **kw: published.append(kw)
        )
        assert await task_dispatch.dispatch_pending_tasks(db) == 1
        second = await advance_source_analysis(db, production_id, step_id)
        assert len(second.task_record_ids) == 1
        next_task = await db.get(UserTaskRecord, second.task_record_ids[0])
        assert next_task.generation_type == "agent_source_global_merge"
        assert await db.get(TaskDispatchOutbox, next_task.id) is not None
