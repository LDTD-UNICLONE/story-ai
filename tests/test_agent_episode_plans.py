from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.core.exceptions import AppException
from app.models.agent_production import (
    AgentCheckpoint,
    AgentProduction,
    AgentStep,
    ProjectSourceDocument,
)
from app.models.project_chapter import ProjectChapter
from app.schemas.agent_production import (
    AgentEpisodePlanConfirmRequest,
    AgentEpisodePlanMergeRequest,
    AgentEpisodePlanSplitRequest,
    AgentEpisodePlanUpdateRequest,
)
from app.services.agent_episode_plans import (
    EpisodePlanContext,
    confirm_episode_plan,
    get_episode_plan_document,
    materialize_episode_chapters,
    merge_episode_plans,
    preview_episode_plan_impact,
    split_episode_plan,
    update_episode_plan,
)


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
    def __init__(self, source, results):
        self.source = source
        self.results = list(results)
        self.added = []
        self.commits = 0

    async def execute(self, _statement):
        return self.results.pop(0)

    async def get(self, model, identifier):
        if model is ProjectSourceDocument and identifier == self.source.id:
            return self.source
        return None

    def add(self, item):
        self.added.append(item)

    async def flush(self):
        for item in self.added:
            if isinstance(item, ProjectChapter) and item.id is None:
                item.id = uuid4()

    async def commit(self):
        self.commits += 1


def _episode(title: str, start: int, end: int) -> dict:
    return {
        "title": title,
        "content": f"{title}的漫剧化内容",
        "logline": f"{title}梗概",
        "opening_hook": "开场异响",
        "goal": "查明真相",
        "conflict": "遭遇阻拦",
        "climax": "正面对峙",
        "ending_hook": "幕后人现身",
        "estimated_duration_seconds": 90,
        "estimated_shot_count": 18,
        "source_start": start,
        "source_end": end,
        "characters": ["主角"],
        "scenes": ["旧宅"],
        "continuity_notes": ["服装连续"],
    }


def _context_objects():
    user_id = uuid4()
    production = AgentProduction(
        id=uuid4(),
        project_id=uuid4(),
        user_id=user_id,
        source_document_id=uuid4(),
        status="waiting_approval",
        current_stage="episode_plan_review",
        mode="supervised",
        production_spec={"text_model_id": str(uuid4())},
        estimated_points=0,
        consumed_points=0,
        lock_version=1,
        extra={},
    )
    step = AgentStep(
        id=uuid4(),
        production_id=production.id,
        stage="source_analysis",
        scope_type="production",
        scope_id=production.id,
        status="waiting_approval",
        input_version=1,
        output_version=1,
        progress_current=4,
        progress_total=4,
        attempt_count=1,
        extra={
            "episode_plan": {
                "planning_summary": "两集结构",
                "episodes": [_episode("第一集", 0, 100), _episode("第二集", 100, 200)],
            }
        },
    )
    checkpoint = AgentCheckpoint(
        id=uuid4(),
        production_id=production.id,
        step_id=step.id,
        checkpoint_type="episode_plan_review",
        status="pending",
        summary="待确认",
        impact={"episode_count": 2},
        extra={},
    )
    source = ProjectSourceDocument(
        id=production.source_document_id,
        project_id=production.project_id,
        user_id=user_id,
        source_type="text",
        content="甲" * 200,
        content_hash="a" * 64,
        character_count=200,
        version=1,
        parse_status="success",
        extra={},
    )
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        production=production,
        step=step,
        checkpoint=checkpoint,
        source=source,
    )


def _session(context, *tail_results):
    return FakeSession(
        context.source,
        [
            FakeResult(scalar=context.production),
            FakeResult(scalar=context.step),
            FakeResult(scalar=context.checkpoint),
            *tail_results,
        ],
    )


def test_episode_plan_confirm_normalizes_and_rejects_blank_idempotency_key() -> None:
    payload = AgentEpisodePlanConfirmRequest(
        expected_version=1,
        idempotency_key="  confirm-v1  ",
    )
    assert payload.idempotency_key == "confirm-v1"

    with pytest.raises(ValidationError):
        AgentEpisodePlanConfirmRequest(
            expected_version=1,
            idempotency_key="        ",
        )


def test_episode_plan_update_accepts_only_complete_source_range() -> None:
    payload = AgentEpisodePlanUpdateRequest(
        expected_version=1,
        source_start=10,
        source_end=20,
    )
    assert (payload.source_start, payload.source_end) == (10, 20)

    with pytest.raises(ValidationError):
        AgentEpisodePlanUpdateRequest(
            expected_version=1,
            source_start=10,
        )


@pytest.mark.asyncio
async def test_materialize_episode_chapters_updates_reused_and_disables_removed() -> None:
    context = _context_objects()
    first_plan_id = uuid4()
    removed_plan_id = uuid4()
    existing = ProjectChapter(
        id=uuid4(),
        project_id=context.production.project_id,
        user_id=context.user.id,
        title="旧标题",
        content="甲" * 80,
        processed_content="旧内容",
        process_status="success",
        sort_order=3,
        is_enabled=True,
        extra={
            "agent_production_id": str(context.production.id),
            "episode_plan_id": str(first_plan_id),
            "source_start": 0,
            "source_end": 80,
            "storyboard_analysis_status": "success",
            "characters": ["旧角色"],
        },
    )
    removed = ProjectChapter(
        id=uuid4(),
        project_id=context.production.project_id,
        user_id=context.user.id,
        title="待移除分集",
        content="甲" * 100,
        processed_content="待移除内容",
        process_status="success",
        sort_order=4,
        is_enabled=True,
        extra={
            "agent_production_id": str(context.production.id),
            "episode_plan_id": str(removed_plan_id),
            "source_start": 100,
            "source_end": 200,
        },
    )
    session = FakeSession(context.source, [FakeResult(items=[existing, removed])])
    item = {
        **_episode("修订后的第一集", 0, 100),
        "plan_id": str(first_plan_id),
        "episode_number": 1,
        "characters": ["主角"],
    }

    chapter_ids, created_count, changed_ids = await materialize_episode_chapters(
        session,
        EpisodePlanContext(
            production=context.production,
            step=context.step,
            checkpoint=context.checkpoint,
            source=context.source,
        ),
        context.user.id,
        [item],
        2,
    )

    assert chapter_ids == [existing.id]
    assert created_count == 0
    assert changed_ids == [existing.id]
    assert existing.title == "修订后的第一集"
    assert existing.content == "甲" * 100
    assert existing.processed_content == item["content"]
    assert existing.sort_order == 0
    assert existing.extra["characters"] == ["主角"]
    assert existing.extra["storyboard_analysis_status"] == "invalidated"
    assert removed.is_enabled is False


@pytest.mark.asyncio
async def test_episode_plan_edit_merge_split_preview_and_idempotent_confirm() -> None:
    context = _context_objects()
    document = await get_episode_plan_document(
        _session(context),
        context.production.id,
        context.user.id,
    )
    assert [item["episode_number"] for item in document["items"]] == [1, 2]
    assert [item["source_content"] for item in document["items"]] == [
        "甲" * 100,
        "甲" * 100,
    ]
    first_id, second_id = [item["plan_id"] for item in document["items"]]

    updated = await update_episode_plan(
        _session(context),
        context.production.id,
        first_id,
        context.user,
        AgentEpisodePlanUpdateRequest(
            expected_version=1,
            title="第一集（修订）",
            position=2,
        ),
    )
    assert updated["version"] == 2
    assert [item["plan_id"] for item in updated["items"]] == [str(second_id), str(first_id)]

    merged = await merge_episode_plans(
        _session(context),
        context.production.id,
        context.user,
        AgentEpisodePlanMergeRequest(
            expected_version=2,
            plan_ids=[second_id, first_id],
            title="合并集",
        ),
    )
    assert merged["version"] == 3
    assert len(merged["items"]) == 1
    merged_id = merged["items"][0]["plan_id"]
    assert merged["items"][0]["source_start"] == 0
    assert merged["items"][0]["source_end"] == 200

    split = await split_episode_plan(
        _session(context),
        context.production.id,
        merged_id,
        context.user,
        AgentEpisodePlanSplitRequest(expected_version=3, split_at=100),
    )
    assert split["version"] == 4
    assert len(split["items"]) == 2
    assert split["items"][0]["source_end"] == split["items"][1]["source_start"] == 100

    impact = await preview_episode_plan_impact(
        _session(context, FakeResult(scalar=0)),
        context.production.id,
        context.user.id,
    )
    assert impact["chapter_count"] == 2
    assert impact["will_create_count"] == 2
    assert impact["source_covered_characters"] == 200

    confirm_session = _session(
        context,
        FakeResult(items=[]),
        FakeResult(scalar=-1),
    )
    confirmed = await confirm_episode_plan(
        confirm_session,
        context.production.id,
        context.user,
        AgentEpisodePlanConfirmRequest(
            expected_version=4,
            idempotency_key="confirm-plan-v4",
        ),
    )
    chapters = [item for item in confirm_session.added if isinstance(item, ProjectChapter)]
    assert confirmed["created_count"] == 2
    assert len(chapters) == 2
    assert chapters[0].content == "甲" * 100
    assert chapters[0].processed_content
    assert context.checkpoint.status == "approved"
    assert context.step.status == "completed"
    assert context.production.status == "planning"
    assert context.production.current_stage == "story_bible"

    repeated = await confirm_episode_plan(
        _session(context),
        context.production.id,
        context.user,
        AgentEpisodePlanConfirmRequest(
            expected_version=4,
            idempotency_key="confirm-plan-v4",
        ),
    )
    assert repeated["already_confirmed"] is True
    assert repeated["created_count"] == 0
    assert repeated["chapter_ids"] == confirmed["chapter_ids"]


@pytest.mark.asyncio
async def test_episode_plan_write_rejects_stale_version() -> None:
    context = _context_objects()
    document = await get_episode_plan_document(
        _session(context),
        context.production.id,
        context.user.id,
    )

    with pytest.raises(AppException) as exc_info:
        await update_episode_plan(
            _session(context),
            context.production.id,
            document["items"][0]["plan_id"],
            context.user,
            AgentEpisodePlanUpdateRequest(expected_version=99, title="冲突修改"),
        )

    assert exc_info.value.status_code == 409
    assert "当前版本为 1" in str(exc_info.value)
    assert exc_info.value.data == {"expected_version": 99, "current_version": 1}


@pytest.mark.asyncio
async def test_episode_plan_confirm_rejects_stale_version() -> None:
    context = _context_objects()

    with pytest.raises(AppException) as exc_info:
        await confirm_episode_plan(
            _session(context),
            context.production.id,
            context.user,
            AgentEpisodePlanConfirmRequest(
                expected_version=2,
                idempotency_key="stale-confirm-v2",
            ),
        )

    assert exc_info.value.code == 40920
    assert context.checkpoint.status == "pending"


@pytest.mark.asyncio
async def test_episode_plan_merge_rejects_non_adjacent_items() -> None:
    context = _context_objects()
    context.step.extra["episode_plan"]["episodes"].append(_episode("第三集", 200, 300))
    context.source.content = "甲" * 300
    context.source.character_count = 300
    document = await get_episode_plan_document(
        _session(context),
        context.production.id,
        context.user.id,
    )

    with pytest.raises(AppException) as exc_info:
        await merge_episode_plans(
            _session(context),
            context.production.id,
            context.user,
            AgentEpisodePlanMergeRequest(
                expected_version=1,
                plan_ids=[document["items"][0]["plan_id"], document["items"][2]["plan_id"]],
            ),
        )

    assert exc_info.value.code == 40055


@pytest.mark.asyncio
async def test_episode_plan_merge_resolves_mixed_source_references() -> None:
    context = _context_objects()
    first = context.step.extra["episode_plan"]["episodes"][0]
    first.pop("source_start")
    first.pop("source_end")
    first["source_block_ids"] = [0]
    context.step.extra["chunks"] = [
        {"index": 0, "start_offset": 0, "end_offset": 100},
    ]
    document = await get_episode_plan_document(
        _session(context),
        context.production.id,
        context.user.id,
    )

    merged = await merge_episode_plans(
        _session(context),
        context.production.id,
        context.user,
        AgentEpisodePlanMergeRequest(
            expected_version=1,
            plan_ids=[item["plan_id"] for item in document["items"]],
        ),
    )

    assert merged["items"][0]["source_start"] == 0
    assert merged["items"][0]["source_end"] == 200


@pytest.mark.asyncio
async def test_episode_plan_split_rejects_boundary_position() -> None:
    context = _context_objects()
    document = await get_episode_plan_document(
        _session(context),
        context.production.id,
        context.user.id,
    )

    with pytest.raises(AppException) as exc_info:
        await split_episode_plan(
            _session(context),
            context.production.id,
            document["items"][0]["plan_id"],
            context.user,
            AgentEpisodePlanSplitRequest(expected_version=1, split_at=100),
        )

    assert exc_info.value.code == 40056


@pytest.mark.asyncio
async def test_episode_plan_impact_warns_about_overlap_and_out_of_bounds() -> None:
    context = _context_objects()
    episodes = context.step.extra["episode_plan"]["episodes"]
    episodes[0]["source_end"] = 120
    episodes[1]["source_start"] = 100
    episodes[1]["source_end"] = 220

    impact = await preview_episode_plan_impact(
        _session(context, FakeResult(scalar=0)),
        context.production.id,
        context.user.id,
    )

    assert impact["source_covered_characters"] == 200
    assert any("重叠" in warning for warning in impact["warnings"])
    assert any("超出原文范围" in warning for warning in impact["warnings"])
