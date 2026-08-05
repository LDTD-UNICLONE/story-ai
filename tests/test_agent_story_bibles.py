from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.core.exceptions import AppException
from app.main import app
from app.models.agent_production import AgentCheckpoint, AgentProduction, AgentStep
from app.models.agent_story_bible import AgentAssetCandidate, SeriesBibleVersion
from app.models.project_asset import ProjectCharacter
from app.schemas.agent_story_bible import (
    AgentAssetCandidateMaterializeRequest,
    AgentAssetCandidateUpdateRequest,
    SeriesBibleConfirmRequest,
)
from app.services.agent_story_bibles import (
    build_asset_candidate_payloads,
    confirm_story_bible,
    materialize_asset_candidates,
    sync_asset_candidate_episode_links,
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
    def __init__(self, results):
        self.results = list(results)
        self.added = []
        self.commits = 0

    async def execute(self, _statement):
        return self.results.pop(0)

    def add(self, item):
        self.added.append(item)

    async def flush(self):
        for item in self.added:
            if isinstance(item, ProjectCharacter) and item.id is None:
                item.id = uuid4()

    async def commit(self):
        self.commits += 1


def _workflow_context(candidate_status: str = "ready"):
    user_id = uuid4()
    production = AgentProduction(
        id=uuid4(),
        project_id=uuid4(),
        user_id=user_id,
        source_document_id=uuid4(),
        status="waiting_approval",
        current_stage="story_bible_review",
        mode="supervised",
        production_spec={},
        estimated_points=0,
        consumed_points=0,
        lock_version=2,
        extra={},
    )
    source_step = AgentStep(
        id=uuid4(),
        production_id=production.id,
        stage="source_analysis",
        scope_type="production",
        scope_id=production.id,
        status="completed",
        input_version=1,
        output_version=1,
        extra={},
    )
    story_step = AgentStep(
        id=uuid4(),
        production_id=production.id,
        stage="story_bible",
        scope_type="production",
        scope_id=production.id,
        status="waiting_approval",
        input_version=1,
        output_version=1,
        extra={},
    )
    checkpoint = AgentCheckpoint(
        id=uuid4(),
        production_id=production.id,
        step_id=story_step.id,
        checkpoint_type="story_bible_review",
        status="pending",
        summary="待确认",
        impact={},
        extra={},
    )
    bible = SeriesBibleVersion(
        id=uuid4(),
        project_id=production.project_id,
        production_id=production.id,
        step_id=story_step.id,
        version=1,
        status="draft",
        content={},
        created_by=user_id,
    )
    candidate = AgentAssetCandidate(
        id=uuid4(),
        project_id=production.project_id,
        production_id=production.id,
        bible_version_id=bible.id,
        user_id=user_id,
        asset_type="character",
        candidate_key="a" * 64,
        canonical_name="沈砚",
        aliases=["阿砚"],
        source_chapter_ids=[],
        confidence=0.93,
        merge_reason="标准名称与别名匹配",
        review_status=candidate_status,
        content={
            "appearance": "黑发，灰色长风衣",
            "personality": "沉稳",
            "states": [{"episode": 1}],
        },
        lock_version=0,
    )
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        production=production,
        source_step=source_step,
        story_step=story_step,
        checkpoint=checkpoint,
        bible=bible,
        candidate=candidate,
    )


def _context_results(context):
    return [
        FakeResult(scalar=context.production),
        FakeResult(scalar=context.source_step),
        FakeResult(scalar=context.story_step),
        FakeResult(scalar=context.checkpoint),
        FakeResult(scalar=context.bible),
    ]


def test_story_bible_routes_are_registered() -> None:
    routes = {
        (path, method.upper())
        for path, methods in app.openapi()["paths"].items()
        for method in methods
    }

    expected = {
        ("/api/v1/agent-productions/{production_id}/story-bible/initialize", "POST"),
        ("/api/v1/agent-productions/{production_id}/story-bible", "GET"),
        ("/api/v1/agent-productions/{production_id}/story-bible/versions", "GET"),
        ("/api/v1/agent-productions/{production_id}/story-bible", "PATCH"),
        ("/api/v1/agent-productions/{production_id}/story-bible/confirm", "POST"),
        ("/api/v1/agent-productions/{production_id}/asset-candidates", "GET"),
        (
            "/api/v1/agent-productions/{production_id}/asset-candidates/{candidate_id}",
            "PATCH",
        ),
        ("/api/v1/agent-productions/{production_id}/asset-candidates/materialize", "POST"),
    }
    assert expected <= routes


def test_candidate_merge_retains_aliases_sources_and_review_threshold() -> None:
    first_chapter_id = uuid4()
    second_chapter_id = uuid4()
    chapters = [
        SimpleNamespace(id=first_chapter_id, extra={"episode_number": 1}),
        SimpleNamespace(id=second_chapter_id, extra={"episode_number": 2}),
    ]
    bible = {
        "characters": [
            {"name": "沈砚", "aliases": ["阿砚"], "appearance": "黑发"},
            {"name": "阿砚", "aliases": ["沈砚"], "personality": "沉稳"},
            {"name": "黑衣人", "aliases": ["影子"]},
            {"name": "刺客", "aliases": ["影子"]},
            {"name": "张三", "aliases": ["他"]},
            {"name": "李四", "aliases": ["他"]},
        ],
        "scenes": [{"name": "旧宅"}, {"name": "旧宅", "atmosphere": "阴冷"}],
        "props": [{"name": "玉佩"}, {"name": "玉佩", "function": "信物"}],
    }
    episode_plan = {
        "episodes": [
            {"episode_number": 1, "characters": ["沈砚"], "scenes": ["旧宅"]},
            {"episode_number": 2, "characters": ["阿砚"], "props": ["玉佩"]},
        ]
    }

    payloads = build_asset_candidate_payloads(bible, episode_plan, chapters)
    by_name = {item["canonical_name"]: item for item in payloads}

    assert by_name["沈砚"]["aliases"] == ["阿砚"]
    assert by_name["沈砚"]["confidence"] == 0.93
    assert by_name["沈砚"]["review_status"] == "ready"
    assert by_name["沈砚"]["source_chapter_ids"] == [
        str(first_chapter_id),
        str(second_chapter_id),
    ]
    assert by_name["黑衣人"]["review_status"] == "needs_review"
    assert "仅别名相交" in by_name["黑衣人"]["merge_reason"]
    assert "张三" in by_name and "李四" in by_name
    assert sum(item["asset_type"] == "scene" for item in payloads) == 1
    assert sum(item["asset_type"] == "prop" for item in payloads) == 1


def test_candidate_episode_links_follow_evidence_and_canonical_rename() -> None:
    context = _workflow_context()
    context.candidate.content = {
        **context.candidate.content,
        "name": "沈砚",
        "source_evidence": [{"source_start": 120, "source_end": 150}],
    }
    episode_plan = {
        "episodes": [
            {
                "episode_number": 1,
                "source_start": 0,
                "source_end": 100,
                "characters": ["错误人物"],
            },
            {
                "episode_number": 2,
                "source_start": 100,
                "source_end": 200,
                "characters": [],
            },
        ]
    }

    sync_asset_candidate_episode_links([context.candidate], episode_plan)
    assert episode_plan["episodes"][0]["characters"] == []
    assert episode_plan["episodes"][1]["characters"] == ["沈砚"]

    context.candidate.canonical_name = "沈彦"
    sync_asset_candidate_episode_links([context.candidate], episode_plan)
    assert episode_plan["episodes"][1]["characters"] == ["沈彦"]


def test_candidate_requests_reject_empty_updates_and_duplicate_ids() -> None:
    with pytest.raises(ValidationError):
        AgentAssetCandidateUpdateRequest(expected_lock_version=0)

    candidate_id = uuid4()
    with pytest.raises(ValidationError):
        AgentAssetCandidateMaterializeRequest(
            expected_bible_version=1,
            candidate_ids=[candidate_id, candidate_id],
        )


@pytest.mark.asyncio
async def test_low_confidence_candidate_must_be_reviewed_before_materialization() -> None:
    context = _workflow_context(candidate_status="needs_review")
    db = FakeSession(
        [
            *_context_results(context),
            FakeResult(items=[context.candidate]),
        ]
    )

    with pytest.raises(AppException) as exc_info:
        await materialize_asset_candidates(
            db,
            context.production.id,
            context.user,
            AgentAssetCandidateMaterializeRequest(
                expected_bible_version=1,
                candidate_ids=[context.candidate.id],
            ),
        )

    assert exc_info.value.status_code == 409
    assert context.candidate.materialized_asset_id is None
    assert db.commits == 0


@pytest.mark.asyncio
async def test_materialization_creates_existing_project_asset_and_is_idempotent() -> None:
    context = _workflow_context()
    first_db = FakeSession(
        [
            *_context_results(context),
            FakeResult(items=[context.candidate]),
            FakeResult(items=[]),
            FakeResult(items=[]),
            FakeResult(items=[]),
        ]
    )
    payload = AgentAssetCandidateMaterializeRequest(
        expected_bible_version=1,
        candidate_ids=[context.candidate.id],
    )

    first = await materialize_asset_candidates(
        first_db,
        context.production.id,
        context.user,
        payload,
    )
    character = next(item for item in first_db.added if isinstance(item, ProjectCharacter))

    assert first["created_count"] == 1
    assert first["reused_count"] == 0
    assert context.candidate.materialized_asset_id == character.id
    assert character.aliases == ["阿砚"]
    assert character.extra["character_states"] == [{"episode": 1}]

    second_db = FakeSession(
        [
            *_context_results(context),
            FakeResult(items=[context.candidate]),
            FakeResult(items=[]),
            FakeResult(items=[]),
            FakeResult(items=[]),
        ]
    )
    second = await materialize_asset_candidates(
        second_db,
        context.production.id,
        context.user,
        payload,
    )

    assert second["created_count"] == 0
    assert second["reused_count"] == 1
    assert second["asset_ids"] == first["asset_ids"]


@pytest.mark.asyncio
async def test_materialization_reuses_alias_match_and_only_fills_blank_fields() -> None:
    context = _workflow_context()
    existing = ProjectCharacter(
        id=uuid4(),
        project_id=context.production.project_id,
        user_id=context.user.id,
        name="阿砚",
        aliases=["沈砚"],
        appearance="用户已确认的白发造型",
        personality=None,
        extra={},
        is_enabled=True,
    )
    db = FakeSession(
        [
            *_context_results(context),
            FakeResult(items=[context.candidate]),
            FakeResult(items=[existing]),
            FakeResult(items=[]),
            FakeResult(items=[]),
        ]
    )

    result = await materialize_asset_candidates(
        db,
        context.production.id,
        context.user,
        AgentAssetCandidateMaterializeRequest(
            expected_bible_version=1,
            candidate_ids=[context.candidate.id],
        ),
    )

    assert result["created_count"] == 0
    assert result["reused_count"] == 1
    assert existing.appearance == "用户已确认的白发造型"
    assert existing.personality == "沉稳"


@pytest.mark.asyncio
async def test_story_bible_confirmation_blocks_unreviewed_and_is_idempotent() -> None:
    context = _workflow_context()
    payload = SeriesBibleConfirmRequest(expected_version=1, idempotency_key="confirm-bible-v1")
    blocked_db = FakeSession([*_context_results(context), FakeResult(scalar=1)])

    with pytest.raises(AppException) as exc_info:
        await confirm_story_bible(
            blocked_db,
            context.production.id,
            context.user,
            payload,
        )
    assert exc_info.value.data == {"needs_review_count": 1}

    confirmed_db = FakeSession(
        [*_context_results(context), FakeResult(scalar=0), FakeResult(scalar=1)]
    )
    first = await confirm_story_bible(
        confirmed_db,
        context.production.id,
        context.user,
        payload,
    )

    assert first["already_confirmed"] is False
    assert context.bible.status == "confirmed"
    assert context.story_step.status == "completed"
    assert context.checkpoint.status == "approved"
    assert context.production.status == "planning"
    assert context.production.current_stage == "core_assets"

    repeated_db = FakeSession([*_context_results(context), FakeResult(scalar=1)])
    repeated = await confirm_story_bible(
        repeated_db,
        context.production.id,
        context.user,
        payload,
    )
    assert repeated["already_confirmed"] is True
