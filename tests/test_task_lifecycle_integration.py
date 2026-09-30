from tests.provider_runtime import isolated_provider_pipeline, recover_and_transfer  # noqa: F401
import asyncio
import importlib
import os
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401
from app.core.exceptions import AppException
from app.core.timezone import beijing_datetime
from app.db.base import Base
from app.models.ai_model import AiModel
from app.models.agent_production import AgentProduction, AgentStep, ProjectSourceDocument
from app.models.conversation import Conversation, ConversationMessage
from app.models.points import UserPointsTransaction
from app.models.project import Project
from app.models.project_asset import ProjectCharacter
from app.models.project_chapter import ProjectChapter
from app.models.project_generated_asset import ProjectGeneratedAsset
from app.models.project_storyboard import ProjectStoryboard
from app.models.style import Style
from app.models.task_record import UserTaskRecord
from app.models.user import User
from app.services.generation import task_records
from app.services.generation import provider_reconciliation as reconciliation
from app.services.projects import lifecycle as projects
from app.services.projects import queries as project_queries
from app.services.billing.model_points import build_model_billing_snapshot
from app.services.generation.runner import ModelRunResult
from app.services.projects.generated_assets import (
    create_project_generated_asset_history,
    list_project_generated_asset_history,
    select_project_generated_asset_history,
)


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_DB_INTEGRATION_TESTS") != "1",
        reason="set RUN_DB_INTEGRATION_TESTS=1 and TEST_DATABASE_URL to a dedicated test database",
    ),
]


@pytest.fixture
async def lifecycle_db(test_database_url):
    url = test_database_url
    schema = f"task_lifecycle_{uuid4().hex}"
    admin = create_async_engine(url, poolclass=NullPool)
    engine = create_async_engine(
        url,
        poolclass=NullPool,
        execution_options={"schema_translate_map": {None: schema}},
    )
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


async def _seed_task(sessions, generation_type="storyboard_image"):
    media_type = "video" if generation_type in {"video", "storyboard_video"} else "image"
    conversation_task = generation_type in {"image", "video"}
    async with sessions() as db:
        user = User(
            id=uuid4(),
            account=uuid4().hex,
            password_hash="test",
            nickname="Test",
            points_balance=90,
        )
        policy = (
            {"type": "video", "default_resolution": "720p", "rates": {"720p": {"none": "2"}}}
            if media_type == "video"
            else {}
        )
        model = AiModel(
            id=uuid4(),
            nickname="Lifecycle test",
            model_id="lifecycle-test",
            vendor="integration" if media_type == "video" else "apimart",
            model_type=media_type,
            is_enabled=True,
            configuration={
                "billing": {"base_points": 6, "multipliers": {"platform": "1"}, "policy": policy}
            },
        )
        db.add_all([user, model])
        await db.flush()
        project = Project(id=uuid4(), user_id=user.id, name="Test", cover="", description="")
        conversation = Conversation(
            id=uuid4(),
            user_id=user.id,
            title="Test",
            conversation_type=media_type,
            ai_model_id=model.id,
        )
        db.add_all([project, conversation])
        await db.flush()
        chapter = ProjectChapter(
            id=uuid4(),
            project_id=project.id,
            user_id=user.id,
            title="Chapter",
            content="Test",
        )
        character = ProjectCharacter(
            id=uuid4(),
            project_id=project.id,
            user_id=user.id,
            name="Character",
            extra={},
        )
        message = ConversationMessage(
            id=uuid4(),
            conversation_id=conversation.id,
            user_id=user.id,
            ai_model_id=model.id,
            role="assistant",
            message_type=media_type,
            content="Pending",
            extra={},
        )
        db.add_all([chapter, character, message])
        await db.flush()
        storyboard = ProjectStoryboard(
            id=uuid4(),
            project_id=project.id,
            chapter_id=chapter.id,
            user_id=user.id,
            title="Shot",
            source_content="Test",
            extra={},
        )
        db.add(storyboard)
        await db.flush()
        record = UserTaskRecord(
            id=uuid4(),
            user_id=user.id,
            ai_model_id=model.id,
            business_type="conversation" if conversation_task else "project",
            business_id=conversation.id if conversation_task else project.id,
            generation_type=generation_type,
            status="running",
            title="Test",
            prompt="Test",
            points_cost=10,
            provider_task_id="provider-task",
            provider_vendor=model.vendor,
            provider_status="running",
            extra={
                "assistant_message_id": str(message.id),
                "chapter_id": str(chapter.id),
                "storyboard_id": str(storyboard.id),
                "asset_id": str(character.id),
                "asset_type": "character",
                "model_extra": {"duration": 3, "resolution": "720p"},
                "model_billing_snapshot": build_model_billing_snapshot(model),
                "points_settled": False,
            },
        )
        db.add(record)
        await db.commit()
        return SimpleNamespace(
            task_id=record.id,
            user_id=user.id,
            project_id=project.id,
            chapter_id=chapter.id,
            model_id=model.id,
            storyboard_id=storyboard.id,
            character_id=character.id,
            message_id=message.id,
            media_type=media_type,
        )


def _result(status="success", url="https://example.com/result.png"):
    return ModelRunResult(
        content=url,
        extra={
            "task_id": "provider-task",
            "task_status": status,
            "credits_cost": "1",
            "oss_last_frame_urls": ["https://example.com/last-frame.png"],
        },
    )


@pytest.fixture
def provider(monkeypatch):
    calls = []

    async def query(*args):
        calls.append(args)
        return _result()

    async def persist(_media_type, result):
        return result

    monkeypatch.setattr(reconciliation, "query_model_task", query)
    monkeypatch.setattr(reconciliation, "persist_generated_media_to_oss", persist)
    return calls


@pytest.mark.parametrize(
    "generation_type",
    [
        "image",
        "video",
        "asset_image_generate",
        "storyboard_image",
        "storyboard_video",
    ],
)
async def test_reconciliation_settles_and_updates_business_once(
    lifecycle_db, provider, generation_type
):
    seed = await _seed_task(lifecycle_db, generation_type)
    for _ in range(2):
        async with lifecycle_db() as db:
            await recover_and_transfer(db, seed.task_id)
    async with lifecycle_db() as db:
        record = await db.get(UserTaskRecord, seed.task_id)
        assert record.status == "success"
        assert record.points_cost == 6
        assert record.extra["points_settled"] is True
        assert record.next_reconcile_at is None
        assert "provider_reconcile_claim_id" not in record.extra
        assert (await db.get(User, seed.user_id)).points_balance == 94
        assert await db.scalar(select(func.count()).select_from(UserPointsTransaction)) == 1
        if generation_type in {"image", "video"}:
            message = await db.get(ConversationMessage, seed.message_id)
            assert message.content == record.result
            assert message.extra["task_status"] == "success"
        else:
            histories = list((await db.scalars(select(ProjectGeneratedAsset))).all())
            assert len(histories) == 1
            assert histories[0].is_selected is True
            assert record.extra["generated_asset_history_id"] == str(histories[0].id)
            if generation_type == "asset_image_generate":
                assert (
                    await db.get(ProjectCharacter, seed.character_id)
                ).reference_image == record.result
            else:
                storyboard = await db.get(ProjectStoryboard, seed.storyboard_id)
                assert storyboard.extra[f"{seed.media_type}_generation_result"] == record.result
    assert len(provider) == 1


@pytest.mark.parametrize(
    "generation_type", ["image", "asset_image_generate", "storyboard_image", "storyboard_video"]
)
async def test_failed_provider_refunds_once(lifecycle_db, provider, monkeypatch, generation_type):
    seed = await _seed_task(lifecycle_db, generation_type)

    async def failed(*_args):
        return _result("failed", "")

    monkeypatch.setattr(reconciliation, "query_model_task", failed)
    for _ in range(2):
        async with lifecycle_db() as db:
            await recover_and_transfer(db, seed.task_id)
    async with lifecycle_db() as db:
        record = await db.get(UserTaskRecord, seed.task_id)
        assert record.status == "failed"
        assert record.extra["refund_transaction_id"]
        assert (await db.get(User, seed.user_id)).points_balance == 100
        assert await db.scalar(select(func.count()).select_from(UserPointsTransaction)) == 1
        assert await db.scalar(select(func.count()).select_from(ProjectGeneratedAsset)) == 0
        if generation_type == "image":
            assert (await db.get(ConversationMessage, seed.message_id)).extra[
                "task_status"
            ] == "failed"
        elif generation_type == "asset_image_generate":
            assert (await db.get(ProjectCharacter, seed.character_id)).extra[
                "image_generation_status"
            ] == "failed"
        else:
            assert (await db.get(ProjectStoryboard, seed.storyboard_id)).extra[
                f"{seed.media_type}_generation_status"
            ] == "failed"


async def test_concurrent_reconciler_observes_committed_claim_without_task_lock(
    lifecycle_db, provider, monkeypatch
):
    seed = await _seed_task(lifecycle_db)
    entered, release = asyncio.Event(), asyncio.Event()

    async def query(*_args):
        entered.set()
        await asyncio.wait_for(release.wait(), timeout=10)
        return _result()

    monkeypatch.setattr(reconciliation, "query_model_task", query)

    async def run():
        async with lifecycle_db() as db:
            return await recover_and_transfer(db, seed.task_id)

    first = asyncio.create_task(run())
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        async with lifecycle_db() as db:
            locked = await db.scalar(
                select(UserTaskRecord)
                .where(UserTaskRecord.id == seed.task_id)
                .with_for_update(nowait=True)
            )
            assert locked.extra["provider_reconcile_claim_id"]
        assert await asyncio.wait_for(run(), timeout=5) is None
    finally:
        release.set()
        await first
    async with lifecycle_db() as db:
        assert await db.scalar(select(func.count()).select_from(ProjectGeneratedAsset)) == 1
        assert await db.scalar(select(func.count()).select_from(UserPointsTransaction)) == 1


async def _expire_claim(sessions, task_id):
    async with sessions() as db:
        record = await db.get(UserTaskRecord, task_id)
        past = beijing_datetime() - timedelta(minutes=1)
        record.next_reconcile_at = past
        record.extra = {**record.extra, "provider_reconcile_claim_until": past.isoformat()}
        await db.commit()


async def test_expired_claim_takeover_discards_old_worker_result(lifecycle_db, provider):
    seed = await _seed_task(lifecycle_db)
    async with lifecycle_db() as db:
        old_claim = await reconciliation._claim_provider_reconcile(db, seed.task_id)
    await _expire_claim(lifecycle_db, seed.task_id)
    async with lifecycle_db() as db:
        new_claim = await reconciliation._claim_provider_reconcile(db, seed.task_id)
        assert new_claim.claim_id != old_claim.claim_id
        assert (
            await reconciliation._finish_provider_reconcile_claim(
                db, old_claim, model_result=_result(), query_failed=False
            )
            is None
        )
    async with lifecycle_db() as db:
        await reconciliation._finish_provider_reconcile_claim(
            db, new_claim, model_result=_result(), query_failed=False
        )
        await reconciliation.transfer_provider_task_media(db, seed.task_id)
    async with lifecycle_db() as db:
        record = await db.get(UserTaskRecord, seed.task_id)
        assert record.status == "success" and record.reconcile_attempts == 2
        assert await db.scalar(select(func.count()).select_from(ProjectGeneratedAsset)) == 1


@pytest.mark.parametrize("failure_stage", ["query", "media", "business"])
async def test_reconciliation_failure_is_recoverable_without_partial_settlement(
    lifecycle_db, provider, monkeypatch, failure_stage
):
    seed = await _seed_task(lifecycle_db)
    target = {
        "query": "query_model_task",
        "media": "persist_generated_media_to_oss",
        "business": "settle_image_task_points",
    }[failure_stage]
    original = getattr(reconciliation, target)

    async def fail(*args, **kwargs):
        if failure_stage == "business":
            await original(*args, **kwargs)
            await args[0].flush()
        raise RuntimeError("simulated persistence failure")

    with monkeypatch.context() as patch:
        patch.setattr(reconciliation, target, fail)
        if failure_stage == "business":
            with pytest.raises(RuntimeError, match="simulated"):
                async with lifecycle_db() as db:
                    await recover_and_transfer(db, seed.task_id)
        else:
            async with lifecycle_db() as db:
                await recover_and_transfer(db, seed.task_id)
    async with lifecycle_db() as db:
        record = await db.get(UserTaskRecord, seed.task_id)
        assert record.status == "running"
        assert record.next_reconcile_at is not None
        assert record.extra["points_settled"] is False
        assert (await db.get(User, seed.user_id)).points_balance == 90
        assert await db.scalar(select(func.count()).select_from(UserPointsTransaction)) == 0
        assert await db.scalar(select(func.count()).select_from(ProjectGeneratedAsset)) == 0
        assert (await db.get(ProjectStoryboard, seed.storyboard_id)).extra == {}
    await _expire_claim(lifecycle_db, seed.task_id)
    async with lifecycle_db() as db:
        await recover_and_transfer(db, seed.task_id)
        assert (await db.get(UserTaskRecord, seed.task_id)).status == "success"


async def test_project_cancellation_obeys_callers_transaction_and_prevents_recovery(
    lifecycle_db, provider
):
    seed = await _seed_task(lifecycle_db)
    async with lifecycle_db() as db:
        claim = await reconciliation._claim_provider_reconcile(db, seed.task_id)
    for commit in (False, True):
        async with lifecycle_db() as db:
            assert (
                await task_records.cancel_project_task_records(
                    db, seed.project_id, seed.user_id, reason="项目已删除"
                )
                == 1
            )
            if commit:
                await db.commit()
            else:
                await db.rollback()
        async with lifecycle_db() as db:
            record = await db.get(UserTaskRecord, seed.task_id)
            assert record.status == ("failed" if commit else "running")
            assert (await db.get(User, seed.user_id)).points_balance == (100 if commit else 90)
            assert await db.scalar(select(func.count()).select_from(UserPointsTransaction)) == int(
                commit
            )
    async with lifecycle_db() as db:
        finished = await reconciliation._finish_provider_reconcile_claim(
            db, claim, model_result=_result(), query_failed=False
        )
        assert finished.status == "failed"
        assert reconciliation.should_reconcile_provider_task(finished) is False
    async with lifecycle_db() as db:
        assert (
            await task_records.cancel_project_task_records(
                db, seed.project_id, seed.user_id, reason="重复删除"
            )
            == 0
        )
        assert (await db.get(UserTaskRecord, seed.task_id)).status == "failed"
        assert await db.scalar(select(func.count()).select_from(ProjectGeneratedAsset)) == 0
        assert await db.scalar(select(func.count()).select_from(UserPointsTransaction)) == 1


@pytest.mark.parametrize("already_expired", [True, False])
async def test_stale_expiration_rechecks_task_before_refunding(lifecycle_db, already_expired):
    seed = await _seed_task(lifecycle_db)
    async with lifecycle_db() as db:
        record = await db.get(UserTaskRecord, seed.task_id)
        record.provider_task_id = None
        record.created_at = beijing_datetime() - timedelta(days=1)
        await db.commit()

    async with lifecycle_db() as first, lifecycle_db() as second:
        first_record = await first.get(UserTaskRecord, seed.task_id)
        second_record = await second.get(UserTaskRecord, seed.task_id)
        if already_expired:
            assert await task_records.expire_stale_task_record(first, first_record) is True
        else:
            first_record.provider_task_id = "accepted-while-expiring"
            await first.commit()
        # The second worker still holds the running record loaded before the first commit.
        assert await task_records.expire_stale_task_record(second, second_record) is False

    async with lifecycle_db() as db:
        assert (await db.get(User, seed.user_id)).points_balance == (100 if already_expired else 90)
        assert await db.scalar(select(func.count()).select_from(UserPointsTransaction)) == int(
            already_expired
        )
        assert await task_records.expire_stale_task_records(db) == 0


@pytest.mark.parametrize("agent", [False, True])
async def test_video_history_keeps_existing_selection_policy(lifecycle_db, provider, agent):
    seed = await _seed_task(lifecycle_db, "storyboard_video")
    async with lifecycle_db() as db:
        record = await db.get(UserTaskRecord, seed.task_id)
        if agent:
            record.extra = {**record.extra, "agent_production_id": str(uuid4())}
        old = await create_project_generated_asset_history(
            db,
            task_record=record,
            target_type="storyboard",
            target_id=seed.storyboard_id,
            media_type="video",
            result_urls=["https://example.com/old.mp4"],
        )
        old_id = old.id
        await db.commit()
    async with lifecycle_db() as db:
        await recover_and_transfer(db, seed.task_id)
    async with lifecycle_db() as db:
        histories = list((await db.scalars(select(ProjectGeneratedAsset))).all())
        assert len(histories) == 2
        selected = [history for history in histories if history.is_selected]
        assert len(selected) == 1
        assert (selected[0].id == old_id) is agent
        storyboard = await db.get(ProjectStoryboard, seed.storyboard_id)
        assert storyboard.extra["video_generation_status"] == (
            "selection_required" if agent else "success"
        )


@pytest.mark.parametrize("project_kind", ["standard", "agent"])
@pytest.mark.parametrize(
    "scenario", ["enabled", "other_owner", "disabled", "missing_style", "disabled_style"]
)
async def test_project_queries_preserve_access_and_style_rules(
    lifecycle_db, project_kind, scenario
):
    seed = await _seed_task(lifecycle_db)
    async with lifecycle_db() as db:
        project = await db.get(Project, seed.project_id)
        project.project_kind = project_kind
        project.is_enabled = scenario != "disabled"
        if scenario != "missing_style":
            style = Style(
                id=uuid4(),
                name=uuid4().hex,
                cover="",
                prompt="Test",
                is_enabled=scenario != "disabled_style",
            )
            db.add(style)
            await db.flush()
            project.style_id = style.id
        await db.commit()
        user_id = uuid4() if scenario == "other_owner" else seed.user_id
        accessible = scenario not in {"other_owner", "disabled"}

        if accessible and project_kind == "standard":
            loaded = await project_queries.get_project_or_404(db, seed.project_id, user_id)
            assert loaded.id == seed.project_id
            assert (loaded.style_id is None) is (scenario == "missing_style")
        else:
            with pytest.raises(AppException) as error:
                await project_queries.get_project_or_404(db, seed.project_id, user_id)
            assert (error.value.code, error.value.status_code) == (40407, 404)

        if accessible and scenario == "enabled":
            loaded = await project_queries.get_owned_enabled_project_with_style_or_404(
                db, seed.project_id, user_id
            )
            assert loaded.id == seed.project_id
            assert loaded.style.id == style.id
            assert loaded.style.is_enabled is True
        else:
            with pytest.raises(AppException) as error:
                await project_queries.get_owned_enabled_project_with_style_or_404(
                    db, seed.project_id, user_id
                )
            assert error.value.code == (40403 if accessible else 40407)
            assert error.value.status_code == 404


async def test_project_queries_list_only_owned_enabled_standard_projects(lifecycle_db):
    seed = await _seed_task(lifecycle_db)
    async with lifecycle_db() as db:
        other = User(id=uuid4(), account=uuid4().hex, nickname="Other", password_hash="test")
        db.add(other)
        await db.flush()
        for kind, enabled, owner in (
            ("agent", True, seed.user_id),
            ("standard", False, seed.user_id),
            ("standard", True, other.id),
        ):
            db.add(
                Project(
                    user_id=owner,
                    name="Hidden",
                    cover="",
                    description="",
                    project_kind=kind,
                    is_enabled=enabled,
                )
            )
        await db.commit()
        items, total = await project_queries.list_projects(db, seed.user_id, None, 1, 20)
        assert total == 1
        assert [item.id for item in items] == [seed.project_id]
        assert await project_queries.list_projects(db, seed.user_id, "Hidden", 1, 20) == ([], 0)


@pytest.mark.parametrize("scenario", ["standard", "agent", "disabled", "other_owner"])
async def test_project_history_access_uses_standard_project_guard(lifecycle_db, scenario):
    seed = await _seed_task(lifecycle_db)
    async with lifecycle_db() as db:
        record = await db.get(UserTaskRecord, seed.task_id)
        history = await create_project_generated_asset_history(
            db,
            task_record=record,
            target_type="storyboard",
            target_id=seed.storyboard_id,
            media_type="image",
            result_urls=["https://example.com/history.png"],
        )
        project = await db.get(Project, seed.project_id)
        project.project_kind = "agent" if scenario == "agent" else "standard"
        project.is_enabled = scenario != "disabled"
        await db.commit()
        user_id = uuid4() if scenario == "other_owner" else seed.user_id
        if scenario == "standard":
            items, total = await list_project_generated_asset_history(
                db,
                project_id=seed.project_id,
                user_id=user_id,
                target_type="storyboard",
                target_id=seed.storyboard_id,
                media_type="image",
                page=1,
                page_size=20,
            )
            assert total == 1 and items[0].id == history.id
            selected, _, _ = await select_project_generated_asset_history(
                db,
                project_id=seed.project_id,
                user_id=user_id,
                history_id=history.id,
            )
            assert selected.id == history.id
        else:
            with pytest.raises(AppException) as error:
                await list_project_generated_asset_history(
                    db,
                    project_id=seed.project_id,
                    user_id=user_id,
                    target_type="storyboard",
                    target_id=seed.storyboard_id,
                    media_type="image",
                    page=1,
                    page_size=20,
                )
            assert error.value.code == 40407
            with pytest.raises(AppException) as error:
                await select_project_generated_asset_history(
                    db,
                    project_id=seed.project_id,
                    user_id=user_id,
                    history_id=history.id,
                )
            assert error.value.code == 40407
            with pytest.raises(AppException) as error:
                await projects.delete_project(db, seed.project_id, user_id)
            assert error.value.code == 40407
            assert record.status == "running"


@pytest.mark.parametrize("commit_fails", [False, True])
async def test_project_deletion_keeps_task_refund_in_same_transaction(
    lifecycle_db, monkeypatch, commit_fails
):
    seed = await _seed_task(lifecycle_db)
    async with lifecycle_db() as db:
        if commit_fails:

            async def fail_commit():
                raise RuntimeError("simulated commit failure")

            monkeypatch.setattr(db, "commit", fail_commit)
            with pytest.raises(RuntimeError, match="simulated commit failure"):
                await projects.delete_project(db, seed.project_id, seed.user_id)
            await db.rollback()
        else:
            deleted = await projects.delete_project(db, seed.project_id, seed.user_id)
            assert deleted.is_enabled is False
    async with lifecycle_db() as db:
        project = await db.get(Project, seed.project_id)
        record = await db.get(UserTaskRecord, seed.task_id)
        assert project.is_enabled is commit_fails
        assert record.status == ("running" if commit_fails else "failed")
        assert (await db.get(User, seed.user_id)).points_balance == (90 if commit_fails else 100)
        assert await db.scalar(select(func.count()).select_from(UserPointsTransaction)) == (
            0 if commit_fails else 1
        )
        if not commit_fails:
            assert record.extra["project_deleted"] is True
            assert record.extra["interrupted"] is True
            with pytest.raises(AppException) as error:
                await projects.delete_project(db, seed.project_id, seed.user_id)
            assert error.value.code == 40407
            assert await db.scalar(select(func.count()).select_from(UserPointsTransaction)) == 1


@pytest.fixture(
    params=[
        "model_generation",
        "project_chapter",
        "project_asset_analysis",
        "project_asset_generation",
        "project_storyboard",
        "project_storyboard_image",
        "project_storyboard_video",
        "agent_source_analysis",
    ]
)
async def direct_worker(lifecycle_db, request):
    name = request.param
    generation_type = {
        "model_generation": "image",
        "project_chapter": "chapter_text_process",
        "project_asset_analysis": "character_analysis",
        "project_asset_generation": "asset_image_generate",
        "project_storyboard": "storyboard_analysis",
        "project_storyboard_image": "storyboard_image",
        "project_storyboard_video": "storyboard_video",
        "agent_source_analysis": "agent_source_analysis",
    }[name]
    seed = await _seed_task(lifecycle_db, generation_type)
    async with lifecycle_db() as db:
        record = await db.get(UserTaskRecord, seed.task_id)
        record.provider_task_id = None
        record.created_at = beijing_datetime() - timedelta(days=1)
        record.extra = {**record.extra, "execution_attempt": 1}
        if name == "agent_source_analysis":
            source = ProjectSourceDocument(
                id=uuid4(),
                project_id=seed.project_id,
                user_id=seed.user_id,
                source_type="text",
                content="Test",
                content_hash=uuid4().hex,
                character_count=4,
                version=1,
                parse_status="running",
            )
            db.add(source)
            await db.flush()
            production = AgentProduction(
                id=uuid4(),
                project_id=seed.project_id,
                user_id=seed.user_id,
                source_document_id=source.id,
                status="running",
                consumed_points=10,
            )
            db.add(production)
            await db.flush()
            step = AgentStep(
                id=uuid4(),
                production_id=production.id,
                stage="source_analysis",
                scope_type="production",
                scope_id=production.id,
                status="running",
                input_version=1,
            )
            db.add(step)
            record.extra = {
                **record.extra,
                "agent_production_id": str(production.id),
                "agent_step_id": str(step.id),
            }
        await db.commit()
    return SimpleNamespace(module=importlib.import_module(f"app.tasks.{name}"), seed=seed)


async def _direct_worker_transition(db, worker, record, transition):
    module = worker.module
    name = module.__name__.rsplit(".", 1)[-1]
    seed = worker.seed
    if name == "agent_source_analysis":
        if transition == "retry":
            await module._mark_retrying(db, record, RuntimeError("late retry"))
        else:
            await module.fail_agent_text_task(db, record, "worker failed", refund=True)
        return
    if name == "model_generation":
        resource = await db.get(ConversationMessage, seed.message_id)
        args = (resource,)
    elif name == "project_asset_generation":
        resource = await db.get(ProjectCharacter, seed.character_id)
        args = (resource, None)
    elif name in {"project_storyboard_image", "project_storyboard_video"}:
        resource = await db.get(ProjectStoryboard, seed.storyboard_id)
        args = (resource,)
    else:
        resource = await db.get(ProjectChapter, seed.chapter_id)
        args = (resource,)
        if name == "project_asset_analysis":
            args += (module.asset_analysis_config("character"),)
    if transition == "retry":
        if name == "project_storyboard":
            await module._mark_retrying(db, record, *args, "late retry", 10)
        else:
            await module._mark_retrying(db, record, *args, "late retry")
    else:
        await module._mark_failed(db, record, *args, "worker failed", refund=True)


async def _direct_worker_state(sessions, seed):
    async with sessions() as db:
        task = await db.get(UserTaskRecord, seed.task_id)
        chapter = await db.get(ProjectChapter, seed.chapter_id)
        storyboard = await db.get(ProjectStoryboard, seed.storyboard_id)
        message = await db.get(ConversationMessage, seed.message_id)
        asset = await db.get(ProjectCharacter, seed.character_id)
        return (
            task.status,
            task.result,
            task.extra,
            (await db.get(User, seed.user_id)).points_balance,
            await db.scalar(select(func.count()).select_from(UserPointsTransaction)),
            chapter.process_status,
            chapter.extra,
            storyboard.extra,
            message.content,
            message.extra,
            asset.extra,
            task.provider_task_id,
            asset.reference_image,
        )


async def test_direct_workers_concurrent_failure_refunds_once(lifecycle_db, direct_worker):
    barrier = asyncio.Barrier(2)

    async def fail():
        async with lifecycle_db() as db:
            task = await db.get(UserTaskRecord, direct_worker.seed.task_id)
            await barrier.wait()
            await _direct_worker_transition(db, direct_worker, task, "fail")

    await asyncio.wait_for(asyncio.gather(fail(), fail()), timeout=10)
    state = await _direct_worker_state(lifecycle_db, direct_worker.seed)
    assert state[0] == "failed"
    assert state[2]["refund_transaction_id"]
    assert state[3:5] == (100, 1)
    if direct_worker.module.__name__.endswith("agent_source_analysis"):
        async with lifecycle_db() as db:
            production = await db.scalar(select(AgentProduction))
            step = await db.scalar(select(AgentStep))
            assert production.consumed_points == 0
            assert production.status == "partially_failed"
            assert step.status == "failed"


@pytest.mark.parametrize("transition", ["fail", "retry"])
@pytest.mark.parametrize("terminal", ["interrupted", "stale", "success"])
async def test_late_worker_cannot_overwrite_terminal_task(
    lifecycle_db, direct_worker, transition, terminal
):
    seed = direct_worker.seed
    async with lifecycle_db() as worker_db:
        task = await worker_db.get(UserTaskRecord, seed.task_id)
        async with lifecycle_db() as db:
            if terminal == "interrupted":
                await task_records.interrupt_task_record(
                    db, seed.task_id, seed.user_id, "admin stop"
                )
            elif terminal == "stale":
                current = await db.get(UserTaskRecord, seed.task_id)
                assert await task_records.expire_stale_task_record(db, current)
            else:
                current = await db.get(UserTaskRecord, seed.task_id, with_for_update=True)
                current.status = "success"
                current.result = "already completed"
                current.extra = {**current.extra, "points_settled": True}
                await db.commit()
        before = await _direct_worker_state(lifecycle_db, seed)
        await _direct_worker_transition(worker_db, direct_worker, task, transition)
    assert await _direct_worker_state(lifecycle_db, seed) == before


async def test_text_worker_late_success_cannot_revive_expired_task(lifecycle_db, monkeypatch):
    from app.tasks import project_chapter

    seed = await _seed_task(lifecycle_db, "chapter_text_process")
    async with lifecycle_db() as db:
        task = await db.get(UserTaskRecord, seed.task_id)
        task.provider_task_id = None
        task.status = "pending"
        task.created_at = beijing_datetime() - timedelta(days=1)
        model = await db.get(AiModel, seed.model_id)
        model.model_type = "text"
        model.vendor = "integration"
        await db.commit()

    async def complete_after_timeout(*args, **kwargs):
        async with lifecycle_db() as db:
            task = await db.get(UserTaskRecord, seed.task_id)
            assert await task_records.expire_stale_task_record(db, task)
        return ModelRunResult(content="late text", extra={})

    monkeypatch.setattr(project_chapter, "WorkerSessionLocal", lifecycle_db)
    monkeypatch.setattr(project_chapter, "run_model", complete_after_timeout)
    await project_chapter._execute_processing(seed.task_id, seed.chapter_id)
    async with lifecycle_db() as db:
        task = await db.get(UserTaskRecord, seed.task_id)
        chapter = await db.get(ProjectChapter, seed.chapter_id)
        assert task.status == "failed"
        assert task.extra["stale_failed"] is True
        assert chapter.process_status == "failed"
        assert chapter.processed_content != "late text"
        assert (await db.get(User, seed.user_id)).points_balance == 100
        assert await db.scalar(select(func.count()).select_from(UserPointsTransaction)) == 1


async def test_worker_refund_rolls_back_with_failure_transaction(
    lifecycle_db, direct_worker, monkeypatch
):
    from app.services.billing import model_points

    before = await _direct_worker_state(lifecycle_db, direct_worker.seed)
    change_points = model_points.change_user_points

    async def refund_then_error(*args, **kwargs):
        await change_points(*args, **kwargs)
        raise RuntimeError("fail after refund flush")

    monkeypatch.setattr(model_points, "change_user_points", refund_then_error)
    async with lifecycle_db() as db:
        task = await db.get(UserTaskRecord, direct_worker.seed.task_id)
        with pytest.raises(RuntimeError, match="fail after refund flush"):
            await _direct_worker_transition(db, direct_worker, task, "fail")
        await db.rollback()
    assert await _direct_worker_state(lifecycle_db, direct_worker.seed) == before


@pytest.mark.parametrize(
    "generation_type",
    [
        "image",
        "asset_image_generate",
        "storyboard_image",
        "storyboard_video",
    ],
)
async def test_worker_late_provider_acceptance_preserves_interruption(
    lifecycle_db, monkeypatch, generation_type
):
    from app.tasks import model_generation
    from app.services.projects import asset_generation, storyboard_images, storyboard_videos

    seed = await _seed_task(lifecycle_db, generation_type)
    async with lifecycle_db() as db:
        record = await db.get(UserTaskRecord, seed.task_id)
        record.provider_task_id = None
        record.status = "pending"
        await db.commit()

    before = None

    async def late_result(*args, **kwargs):
        nonlocal before
        async with lifecycle_db() as db:
            await task_records.interrupt_task_record(db, seed.task_id, seed.user_id, "stop")
        before = await _direct_worker_state(lifecycle_db, seed)
        return ModelRunResult(content="processing", extra={"task_id": "late-provider-id"})

    module = {
        "image": model_generation,
        "asset_image_generate": asset_generation,
        "storyboard_image": storyboard_images,
        "storyboard_video": storyboard_videos,
    }[generation_type]
    monkeypatch.setattr(module, "run_model", late_result)
    if generation_type == "image":
        monkeypatch.setattr(module, "WorkerSessionLocal", lifecycle_db)
        await module._execute_generation(seed.task_id, seed.message_id)
    else:
        async with lifecycle_db() as db:
            record = await db.get(UserTaskRecord, seed.task_id)
            if generation_type == "asset_image_generate":
                await module.run_asset_image_generation_in_worker(
                    db, record, "character", seed.character_id
                )
            elif generation_type == "storyboard_image":
                await module.run_storyboard_image_generation_in_worker(
                    db, record, seed.storyboard_id
                )
            else:
                await module.run_storyboard_video_generation_in_worker(
                    db, record, seed.storyboard_id
                )
            await db.commit()
    assert before is not None
    assert await _direct_worker_state(lifecycle_db, seed) == before


async def test_streaming_text_does_not_write_after_interruption(lifecycle_db):
    from app.tasks.model_generation import _build_apimart_text_delta_callback

    seed = await _seed_task(lifecycle_db, "image")
    async with lifecycle_db() as db:
        record = await db.get(UserTaskRecord, seed.task_id)
        message = await db.get(ConversationMessage, seed.message_id)
        callback = _build_apimart_text_delta_callback(db, message, record)
        await callback("first " * 32)
        async with lifecycle_db() as other:
            await task_records.interrupt_task_record(
                other, seed.task_id, seed.user_id, "stop stream"
            )
        before = await _direct_worker_state(lifecycle_db, seed)
        await callback("late " * 32)
        await callback("ignored " * 32)
    assert await _direct_worker_state(lifecycle_db, seed) == before


async def test_failed_worker_discards_partial_generated_history(lifecycle_db, monkeypatch):
    from app.tasks import project_storyboard_image
    from app.services.projects import storyboard_images

    seed = await _seed_task(lifecycle_db, "storyboard_image")
    async with lifecycle_db() as db:
        record = await db.get(UserTaskRecord, seed.task_id)
        record.provider_task_id = None
        record.status = "pending"
        await db.commit()

    async def model_result(*args, **kwargs):
        return ModelRunResult(content="https://example.com/result.png", extra={})

    async def persist(_kind, result):
        return result

    async def fail_after_history(db, *args, **kwargs):
        assert await db.scalar(select(func.count()).select_from(ProjectGeneratedAsset)) == 1
        raise RuntimeError("settlement failed after history write")

    monkeypatch.setattr(project_storyboard_image, "WorkerSessionLocal", lifecycle_db)
    monkeypatch.setattr(storyboard_images, "run_model", model_result)
    monkeypatch.setattr(storyboard_images, "persist_generated_media_to_oss", persist)
    monkeypatch.setattr(storyboard_images, "settle_image_task_points", fail_after_history)
    await project_storyboard_image._execute_generation(seed.task_id, seed.storyboard_id)
    async with lifecycle_db() as db:
        record = await db.get(UserTaskRecord, seed.task_id)
        storyboard = await db.get(ProjectStoryboard, seed.storyboard_id)
        assert record.status == "failed"
        assert record.extra["raw_failed_reason"] == "settlement failed after history write"
        assert (await db.get(User, seed.user_id)).points_balance == 100
        assert await db.scalar(select(func.count()).select_from(UserPointsTransaction)) == 1
        assert await db.scalar(select(func.count()).select_from(ProjectGeneratedAsset)) == 0
        assert "image_generation_result" not in storyboard.extra
        assert storyboard.extra["image_generation_status"] == "failed"


async def test_text_worker_holds_task_lock_through_settlement(lifecycle_db, monkeypatch):
    from sqlalchemy.exc import DBAPIError
    from app.tasks import project_chapter

    seed = await _seed_task(lifecycle_db, "chapter_text_process")
    async with lifecycle_db() as db:
        task = await db.get(UserTaskRecord, seed.task_id)
        task.provider_task_id = None
        task.status = "pending"
        model = await db.get(AiModel, seed.model_id)
        model.model_type = "text"
        model.vendor = "integration"
        await db.commit()

    async def model_result(*args, **kwargs):
        # The provider call itself must not hold the task row lock.
        async with lifecycle_db() as db:
            assert (
                await db.scalar(
                    select(UserTaskRecord)
                    .where(UserTaskRecord.id == seed.task_id)
                    .with_for_update(nowait=True)
                )
                is not None
            )
        return ModelRunResult(content="completed text", extra={})

    settle = project_chapter.settle_text_task_points

    async def settle_under_lock(*args, **kwargs):
        async with lifecycle_db() as db:
            with pytest.raises(DBAPIError) as error:
                await db.scalar(
                    select(UserTaskRecord)
                    .where(UserTaskRecord.id == seed.task_id)
                    .with_for_update(nowait=True)
                )
            assert error.value.orig.sqlstate == "55P03"
        await settle(*args, **kwargs)

    monkeypatch.setattr(project_chapter, "WorkerSessionLocal", lifecycle_db)
    monkeypatch.setattr(project_chapter, "run_model", model_result)
    monkeypatch.setattr(project_chapter, "settle_text_task_points", settle_under_lock)
    await project_chapter._execute_processing(seed.task_id, seed.chapter_id)
    async with lifecycle_db() as db:
        task = await db.get(UserTaskRecord, seed.task_id)
        assert task.status == "success"
        assert task.extra["points_settled"] is True
        with pytest.raises(AppException, match="任务已结束"):
            await task_records.interrupt_task_record(db, seed.task_id, seed.user_id)


async def test_text_stream_interruption_completes_without_late_failure(lifecycle_db, monkeypatch):
    from unittest.mock import AsyncMock
    from app.tasks import model_generation

    seed = await _seed_task(lifecycle_db, "image")
    async with lifecycle_db() as db:
        task = await db.get(UserTaskRecord, seed.task_id)
        task.status = "pending"
        task.generation_type = "text"
        task.provider_task_id = None
        model = await db.get(AiModel, seed.model_id)
        model.model_type = "text"
        message = await db.get(ConversationMessage, seed.message_id)
        message.message_type = "text"
        await db.commit()
    before = None

    async def stream_then_stop(*args, **kwargs):
        nonlocal before
        delta = kwargs["on_text_delta"]
        await delta("first " * 32)
        async with lifecycle_db() as db:
            await task_records.interrupt_task_record(db, seed.task_id, seed.user_id, "stop stream")
        before = await _direct_worker_state(lifecycle_db, seed)
        await delta("late " * 32)
        return ModelRunResult(content="late complete text", extra={})

    mark_failed = AsyncMock(wraps=model_generation._mark_failed)
    monkeypatch.setattr(model_generation, "WorkerSessionLocal", lifecycle_db)
    monkeypatch.setattr(model_generation, "run_model", stream_then_stop)
    monkeypatch.setattr(model_generation, "_mark_failed", mark_failed)
    await model_generation._execute_generation(seed.task_id, seed.message_id)
    assert before is not None
    assert await _direct_worker_state(lifecycle_db, seed) == before
    mark_failed.assert_not_awaited()
