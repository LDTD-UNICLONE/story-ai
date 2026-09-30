import json
import os
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.core.exceptions import AppException
from app.models.ai_model import AiModel
from app.models.points import UserPointsTransaction
from app.models.project_chapter import ProjectChapter
from app.models.project_generated_asset import ProjectGeneratedAsset
from app.models.project_storyboard import ProjectStoryboard
from app.models.task_dispatch import TaskDispatchOutbox
from app.models.task_record import UserTaskRecord
from app.models.user import User
from app.schemas.project_storyboard import (
    ProjectStoryboardAnalyzeRequest,
    ProjectStoryboardCreateRequest,
    ProjectStoryboardMergeRequest,
    ProjectStoryboardPromptRequest,
    ProjectStoryboardRefineRequest,
    ProjectStoryboardSplitRequest,
    ProjectStoryboardUpdateRequest,
)
from app.services.generation import task_dispatch
from app.services.generation.runner import ModelRunResult
from app.services.projects import storyboard_execution, storyboard_submission, storyboards
from test_task_lifecycle_integration import _seed_task, lifecycle_db as lifecycle_db


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_DB_INTEGRATION_TESTS") != "1", reason="requires test PostgreSQL"
    ),
]


@pytest.mark.asyncio
async def test_storyboard_edit_order_merge_split_and_access(lifecycle_db):
    seed = await _seed_task(lifecycle_db)
    scope = (seed.project_id, seed.chapter_id, seed.user_id)
    async with lifecycle_db() as db:
        inserted = await storyboards.create_project_storyboard(
            db,
            *scope,
            ProjectStoryboardCreateRequest(title="新增", source_content="进门", shot_number=1),
        )
        inserted_id = inserted.id
        rows = await storyboards.list_enabled_storyboards(db, *scope)
        assert [(r.id, r.shot_number) for r in rows] == [(inserted_id, 1), (seed.storyboard_id, 2)]
        await storyboards.update_project_storyboard(
            db,
            seed.project_id,
            seed.chapter_id,
            inserted_id,
            seed.user_id,
            ProjectStoryboardUpdateRequest(shot_number=2),
        )
        rows, total = await storyboards.list_project_storyboards(db, *scope, page=2, page_size=1)
        assert total == 2 and rows[0].id == inserted_id
        with pytest.raises(AppException) as denied:
            await storyboards.get_project_storyboard_or_404(
                db, seed.project_id, seed.chapter_id, inserted_id, uuid4()
            )
        assert denied.value.status_code == 404
        (merged,) = await storyboards.merge_project_storyboards(
            db,
            *scope,
            ProjectStoryboardMergeRequest(storyboard_ids=[inserted_id, seed.storyboard_id]),
        )
        assert merged.source_content == "Test\n进门"
        assert merged.shot_number == 1
        merged_id = merged.id
        split = await storyboards.split_project_storyboard(
            db,
            seed.project_id,
            seed.chapter_id,
            merged_id,
            seed.user_id,
            ProjectStoryboardSplitRequest(
                units=[
                    {"title": "甲", "source_content": "Test", "action": "站立"},
                    {"title": "乙", "source_content": "进门", "action": "推门"},
                ]
            ),
        )
        assert [(r.title, r.shot_number) for r in split] == [("甲", 1), ("乙", 2)]
        assert not (await db.get(ProjectStoryboard, merged_id)).is_enabled
        await storyboards.delete_project_storyboard(
            db, seed.project_id, seed.chapter_id, split[0].id, seed.user_id
        )
        remaining = await storyboards.list_enabled_storyboards(db, *scope)
        assert [(r.title, r.shot_number) for r in remaining] == [("乙", 1)]


@pytest.mark.asyncio
async def test_storyboard_edit_history_and_fields_roll_back_together(lifecycle_db, monkeypatch):
    seed = await _seed_task(lifecycle_db)
    async with lifecycle_db() as db:
        board = await db.get(ProjectStoryboard, seed.storyboard_id)
        board.extra = {"image_generation_status": "selected"}
        history = ProjectGeneratedAsset(
            id=uuid4(),
            project_id=seed.project_id,
            user_id=seed.user_id,
            target_type="storyboard",
            target_id=seed.storyboard_id,
            media_type="image",
            extra={},
        )
        db.add(history)
        await db.commit()
        history_id = history.id

        async def fail_commit():
            await db.flush()
            raise RuntimeError("injected commit failure")

        with monkeypatch.context() as patch:
            patch.setattr(db, "commit", fail_commit)
            with pytest.raises(RuntimeError, match="injected"):
                await storyboards.update_project_storyboard(
                    db,
                    seed.project_id,
                    seed.chapter_id,
                    seed.storyboard_id,
                    seed.user_id,
                    ProjectStoryboardUpdateRequest(title="修改"),
                )
        await db.rollback()
    async with lifecycle_db() as db:
        board = await db.get(ProjectStoryboard, seed.storyboard_id)
        assert board.title == "Shot"
        assert board.extra["image_generation_status"] == "selected"
        assert (await db.get(ProjectGeneratedAsset, history_id)).extra == {}
        await storyboards.update_project_storyboard(
            db,
            seed.project_id,
            seed.chapter_id,
            seed.storyboard_id,
            seed.user_id,
            ProjectStoryboardUpdateRequest(title="修改"),
        )
    async with lifecycle_db() as db:
        assert (await db.get(ProjectStoryboard, seed.storyboard_id)).extra[
            "image_generation_status"
        ] == "invalidated"
        assert (await db.get(ProjectGeneratedAsset, history_id)).extra[
            "invalidated_by"
        ] == "storyboard_edit"


@pytest.mark.asyncio
async def test_storyboard_delete_cancels_task_and_refunds_once(lifecycle_db):
    seed = await _seed_task(lifecycle_db)
    async with lifecycle_db() as db:
        await storyboards.delete_project_storyboard(
            db, seed.project_id, seed.chapter_id, seed.storyboard_id, seed.user_id
        )
    async with lifecycle_db() as db:
        assert not (await db.get(ProjectStoryboard, seed.storyboard_id)).is_enabled
        assert (await db.get(UserTaskRecord, seed.task_id)).status == "failed"
        assert (await db.get(User, seed.user_id)).points_balance == 100
        assert await db.scalar(select(func.count()).select_from(UserPointsTransaction)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["analysis", "refinement", "image_prompt"])
async def test_storyboard_submission_commits_intent_before_broker_failure(
    lifecycle_db, monkeypatch, stage
):
    seed = await _seed_task(lifecycle_db)

    def unavailable(**kwargs):
        raise ConnectionError("test broker unavailable")

    monkeypatch.setattr(task_dispatch, "publish_task_message", unavailable)
    async with lifecycle_db() as db:
        model = await db.get(AiModel, seed.model_id)
        model.model_type = "text"
        chapter = await db.get(ProjectChapter, seed.chapter_id)
        chapter.processed_content = "他走进旧宅。"
        await db.commit()
        user = await db.get(User, seed.user_id)
        common = (db, seed.project_id, seed.chapter_id)
        if stage == "analysis":
            record, points = await storyboard_submission.submit_storyboard_analysis(
                *common, user, ProjectStoryboardAnalyzeRequest(ai_model_id=seed.model_id)
            )
        elif stage == "refinement":
            record, points = await storyboard_submission.submit_storyboard_refinement(
                *common,
                seed.storyboard_id,
                user,
                ProjectStoryboardRefineRequest(ai_model_id=seed.model_id),
            )
        else:
            record, points = await storyboard_submission.submit_storyboard_image_prompt_generation(
                *common,
                seed.storyboard_id,
                user,
                ProjectStoryboardPromptRequest(ai_model_id=seed.model_id),
            )
        record_id = record.id
        assert points == 0 and record.status == "pending"
    async with lifecycle_db() as db:
        saved = await db.get(UserTaskRecord, record_id)
        intent = (await db.scalars(select(TaskDispatchOutbox))).one()
        assert saved.status == "pending"
        assert intent.id == record_id
        assert intent.queue == "story_ai_text"
        assert intent.args == [str(record_id), str(seed.chapter_id)]
        assert intent.attempt_count == 1
        assert (await db.get(User, seed.user_id)).points_balance == 90
        status_key = (
            "storyboard_image_prompt_generation_status"
            if stage == "image_prompt"
            else f"storyboard_{stage}_status"
        )
        assert (await db.get(ProjectChapter, seed.chapter_id)).extra[status_key] == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("commit", [False, True])
async def test_storyboard_worker_result_and_settlement_follow_caller_transaction(
    lifecycle_db, monkeypatch, commit
):
    seed = await _seed_task(lifecycle_db, "storyboard_analysis")
    async with lifecycle_db() as db:
        model = await db.get(AiModel, seed.model_id)
        model.model_type = "text"
        record = await db.get(UserTaskRecord, seed.task_id)
        record.extra = {"chapter_id": str(seed.chapter_id), "points_settled": False}
        await db.commit()

    async def generated(*args, **kwargs):
        return ModelRunResult(
            content=json.dumps(
                {"storyboard_units": [{"title": "新分镜", "source_content": "剧情"}]}
            ),
            extra={"credits_cost": "1"},
        )

    monkeypatch.setattr(storyboard_execution, "run_model", generated)
    async with lifecycle_db() as db:
        record = await db.get(UserTaskRecord, seed.task_id)
        chapter = await db.get(ProjectChapter, seed.chapter_id)
        await storyboard_execution.run_storyboard_analysis_in_worker(db, record, chapter)
        assert record.status == "success"
        assert record.extra["points_settled"] is True
        assert record.points_cost != 10
        await db.flush()
        async with lifecycle_db() as observer:
            assert (await observer.get(UserTaskRecord, seed.task_id)).status == "running"
            assert (await observer.get(ProjectStoryboard, seed.storyboard_id)).is_enabled
        if commit:
            await db.commit()
        else:
            await db.rollback()
    async with lifecycle_db() as db:
        record = await db.get(UserTaskRecord, seed.task_id)
        rows = await storyboards.list_enabled_storyboards(
            db, seed.project_id, seed.chapter_id, seed.user_id
        )
        balance = (await db.get(User, seed.user_id)).points_balance
        transactions = await db.scalar(select(func.count()).select_from(UserPointsTransaction))
        if commit:
            assert record.status == "success"
            assert [(r.title, r.shot_number) for r in rows] == [("新分镜", 1)]
            assert balance == 100 - record.points_cost
            assert transactions == 1
        else:
            assert record.status == "running" and record.points_cost == 10
            assert [r.id for r in rows] == [seed.storyboard_id]
            assert balance == 90 and transactions == 0
