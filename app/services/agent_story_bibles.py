import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException
from app.core.timezone import beijing_datetime
from app.models.agent_production import AgentCheckpoint, AgentEvent, AgentProduction, AgentStep
from app.models.agent_story_bible import (
    AGENT_ASSET_VARIANT_TYPES,
    AgentAssetCandidate,
    AgentAssetVariant,
    SeriesBibleVersion,
)
from app.models.project import Project
from app.models.project_asset import ProjectCharacter, ProjectProp, ProjectScene
from app.models.project_chapter import ProjectChapter
from app.models.style import Style
from app.models.user import User
from app.schemas.agent_story_bible import (
    AgentAssetCandidateMaterializeRequest,
    AgentAssetCandidateUpdateRequest,
    SeriesBibleConfirmRequest,
    SeriesBibleUpdateRequest,
)
from app.services.agent_production_state import (
    transition_checkpoint_status,
    transition_production_status,
    transition_step_status,
)
from app.services.agent_workflow import bump_script_review_version, uses_agent_workflow_v2


REVIEW_CONFIDENCE_THRESHOLD = 0.8
ASSET_TYPES = {"character", "scene", "prop"}
_WEAK_ALIASES = {"他", "她", "它", "男人", "女人", "男主", "女主", "主角", "神秘人"}


@dataclass
class StoryBibleContext:
    production: AgentProduction
    source_step: AgentStep
    story_step: Optional[AgentStep] = None
    checkpoint: Optional[AgentCheckpoint] = None
    bible: Optional[SeriesBibleVersion] = None


async def initialize_script_assets(
    db: AsyncSession,
    production: AgentProduction,
    source_step: AgentStep,
    global_analysis: Dict[str, Any],
    episode_plan: Dict[str, Any],
) -> Tuple[
    SeriesBibleVersion,
    List[AgentAssetCandidate],
    List[AgentAssetVariant],
    List[Dict[str, Any]],
]:
    existing_result = await db.execute(
        select(SeriesBibleVersion)
        .where(SeriesBibleVersion.production_id == production.id)
        .order_by(SeriesBibleVersion.version.desc())
        .limit(1)
    )
    existing = existing_result.scalar_one_or_none()
    if existing is not None:
        candidate_result = await db.execute(
            select(AgentAssetCandidate).where(
                AgentAssetCandidate.bible_version_id == existing.id
            )
        )
        variant_result = await db.execute(
            select(AgentAssetVariant).where(
                AgentAssetVariant.bible_version_id == existing.id
            )
        )
        previous_candidates = list(candidate_result.scalars().all())
        previous_variants = list(variant_result.scalars().all())
        if existing.step_id == source_step.id:
            return existing, previous_candidates, previous_variants, []

        analyzed_content = _ensure_variant_base_assets(
            await _build_bible_content_from_analysis(db, production, global_analysis)
        )
        previous_content = _content_with_reviewed_assets(
            existing.content or {},
            previous_candidates,
            previous_variants,
        )
        content = _merge_incremental_bible_content(previous_content, analyzed_content)
        bible = SeriesBibleVersion(
            project_id=production.project_id,
            production_id=production.id,
            step_id=source_step.id,
            version=existing.version + 1,
            status="draft",
            content=content,
            created_by=production.user_id,
        )
        db.add(bible)
        await db.flush()
        candidates = [
            AgentAssetCandidate(
                project_id=production.project_id,
                production_id=production.id,
                bible_version_id=bible.id,
                user_id=production.user_id,
                **payload,
            )
            for payload in build_asset_candidate_payloads(content, episode_plan, [])
        ]
        for candidate in candidates:
            db.add(candidate)
        await db.flush()
        sync_asset_candidate_episode_links(candidates, episode_plan)
        variant_payloads, warnings = build_asset_variant_payloads(
            content,
            episode_plan,
            candidates,
        )
        variants = [
            AgentAssetVariant(
                project_id=production.project_id,
                production_id=production.id,
                bible_version_id=bible.id,
                user_id=production.user_id,
                **payload,
            )
            for payload in variant_payloads
        ]
        for variant in variants:
            db.add(variant)
        sync_asset_variant_episode_links(variants, episode_plan)
        existing.status = "superseded"
        return bible, candidates, variants, warnings

    content = _ensure_variant_base_assets(
        await _build_bible_content_from_analysis(db, production, global_analysis)
    )
    bible = SeriesBibleVersion(
        project_id=production.project_id,
        production_id=production.id,
        step_id=source_step.id,
        version=1,
        status="draft",
        content=content,
        created_by=production.user_id,
    )
    db.add(bible)
    await db.flush()
    candidates = [
        AgentAssetCandidate(
            project_id=production.project_id,
            production_id=production.id,
            bible_version_id=bible.id,
            user_id=production.user_id,
            **payload,
        )
        for payload in build_asset_candidate_payloads(content, episode_plan, [])
    ]
    for candidate in candidates:
        db.add(candidate)
    await db.flush()
    sync_asset_candidate_episode_links(candidates, episode_plan)
    variant_payloads, warnings = build_asset_variant_payloads(
        content,
        episode_plan,
        candidates,
    )
    variants = [
        AgentAssetVariant(
            project_id=production.project_id,
            production_id=production.id,
            bible_version_id=bible.id,
            user_id=production.user_id,
            **payload,
        )
        for payload in variant_payloads
    ]
    for variant in variants:
        db.add(variant)
    sync_asset_variant_episode_links(variants, episode_plan)
    return bible, candidates, variants, warnings


async def initialize_story_bible(
    db: AsyncSession,
    production_id: UUID,
    user: User,
) -> SeriesBibleVersion:
    context = await _get_context(db, production_id, user.id, lock=True, require_bible=False)
    if context.bible is not None:
        return context.bible
    if context.production.status != "planning" or context.production.current_stage != "story_bible":
        raise AppException("当前整剧状态不允许初始化故事圣经", code=40921, status_code=409)
    if context.source_step.status != "completed":
        raise AppException("分集规划尚未确认", code=40922, status_code=409)

    story_step = AgentStep(
        production_id=context.production.id,
        stage="story_bible",
        scope_type="production",
        scope_id=context.production.id,
        status="waiting_approval",
        input_version=int(context.source_step.output_version or 1),
        output_version=1,
        progress_current=1,
        progress_total=1,
        attempt_count=1,
        started_at=beijing_datetime(),
        finished_at=beijing_datetime(),
        extra={},
    )
    db.add(story_step)
    await db.flush()
    content = await _build_bible_content(db, context)
    bible = SeriesBibleVersion(
        project_id=context.production.project_id,
        production_id=context.production.id,
        step_id=story_step.id,
        version=1,
        status="draft",
        content=content,
        created_by=user.id,
    )
    db.add(bible)
    await db.flush()

    chapters = await _production_chapters(db, context.production)
    candidate_payloads = build_asset_candidate_payloads(
        content,
        (context.source_step.extra or {}).get("episode_plan") or {},
        chapters,
    )
    for payload in candidate_payloads:
        db.add(
            AgentAssetCandidate(
                project_id=context.production.project_id,
                production_id=context.production.id,
                bible_version_id=bible.id,
                user_id=user.id,
                **payload,
            )
        )

    review_count = sum(payload["review_status"] == "needs_review" for payload in candidate_payloads)
    checkpoint = AgentCheckpoint(
        production_id=context.production.id,
        step_id=story_step.id,
        checkpoint_type="story_bible_review",
        status="pending",
        summary=f"故事圣经第 1 版已生成，共 {len(candidate_payloads)} 个资产候选。",
        impact={
            "bible_version": 1,
            "candidate_count": len(candidate_payloads),
            "needs_review_count": review_count,
        },
        extra={"bible_version_id": str(bible.id)},
    )
    db.add(checkpoint)
    story_step.extra = {"current_bible_version_id": str(bible.id)}
    context.production.status = transition_production_status(
        context.production.status,
        "waiting_approval",
    )
    context.production.current_stage = "story_bible_review"
    context.production.lock_version += 1
    db.add(
        AgentEvent(
            production_id=context.production.id,
            step_id=story_step.id,
            actor_user_id=user.id,
            event_type="story_bible.initialized",
            source="user",
            payload={
                "bible_version_id": str(bible.id),
                "candidate_count": len(candidate_payloads),
                "needs_review_count": review_count,
            },
        )
    )
    await db.commit()
    await db.refresh(bible)
    return bible


async def get_current_story_bible(
    db: AsyncSession,
    production_id: UUID,
    user_id: UUID,
) -> SeriesBibleVersion:
    context = await _get_context(db, production_id, user_id, lock=False, require_bible=True)
    return context.bible  # type: ignore[return-value]


async def list_story_bible_versions(
    db: AsyncSession,
    production_id: UUID,
    user_id: UUID,
) -> List[SeriesBibleVersion]:
    production = await _get_production(db, production_id, user_id, lock=False)
    result = await db.execute(
        select(SeriesBibleVersion)
        .where(SeriesBibleVersion.production_id == production.id)
        .order_by(SeriesBibleVersion.version.desc())
    )
    return list(result.scalars().all())


async def update_story_bible(
    db: AsyncSession,
    production_id: UUID,
    user: User,
    payload: SeriesBibleUpdateRequest,
) -> SeriesBibleVersion:
    context = await _get_context(db, production_id, user.id, lock=True, require_bible=True)
    _reject_legacy_v2_operation(context)
    bible = _editable_bible(context)
    _check_bible_version(bible, payload.expected_version)
    materialized_result = await db.execute(
        select(func.count())
        .select_from(AgentAssetCandidate)
        .where(
            AgentAssetCandidate.bible_version_id == bible.id,
            AgentAssetCandidate.materialized_asset_id.is_not(None),
        )
    )
    if int(materialized_result.scalar_one() or 0):
        raise AppException(
            "该故事圣经版本已物化资产，不能直接创建修订版",
            code=40923,
            status_code=409,
        )

    new_version = bible.version + 1
    new_bible = SeriesBibleVersion(
        project_id=bible.project_id,
        production_id=bible.production_id,
        step_id=bible.step_id,
        version=new_version,
        status="draft",
        content={**(bible.content or {}), **payload.content},
        created_by=user.id,
    )
    db.add(new_bible)
    await db.flush()
    candidate_result = await db.execute(
        select(AgentAssetCandidate).where(AgentAssetCandidate.bible_version_id == bible.id)
    )
    for candidate in candidate_result.scalars().all():
        db.add(
            AgentAssetCandidate(
                project_id=candidate.project_id,
                production_id=candidate.production_id,
                bible_version_id=new_bible.id,
                user_id=candidate.user_id,
                asset_type=candidate.asset_type,
                candidate_key=candidate.candidate_key,
                canonical_name=candidate.canonical_name,
                aliases=list(candidate.aliases or []),
                source_chapter_ids=list(candidate.source_chapter_ids or []),
                confidence=candidate.confidence,
                merge_reason=candidate.merge_reason,
                review_status=candidate.review_status,
                content=dict(candidate.content or {}),
            )
        )
    bible.status = "superseded"
    story_step = context.story_step
    checkpoint = context.checkpoint
    if story_step is None or checkpoint is None:
        raise AppException("故事圣经步骤数据不完整", code=50047, status_code=500)
    story_step.output_version = new_version
    story_step.extra = {**(story_step.extra or {}), "current_bible_version_id": str(new_bible.id)}
    checkpoint.summary = f"故事圣经已更新为第 {new_version} 版，请审核。"
    checkpoint.impact = {**(checkpoint.impact or {}), "bible_version": new_version}
    checkpoint.extra = {**(checkpoint.extra or {}), "bible_version_id": str(new_bible.id)}
    context.production.lock_version += 1
    db.add(
        AgentEvent(
            production_id=context.production.id,
            step_id=story_step.id,
            actor_user_id=user.id,
            event_type="story_bible.updated",
            source="user",
            payload={"from_version": bible.version, "to_version": new_version},
        )
    )
    await db.commit()
    await db.refresh(new_bible)
    return new_bible


async def list_asset_candidates(
    db: AsyncSession,
    production_id: UUID,
    user_id: UUID,
    *,
    asset_type: Optional[str] = None,
    review_status: Optional[str] = None,
) -> Tuple[int, List[AgentAssetCandidate]]:
    context = await _get_context(db, production_id, user_id, lock=False, require_bible=True)
    bible = context.bible
    if bible is None:
        raise AppException("故事圣经尚未生成", code=40924, status_code=409)
    conditions = [AgentAssetCandidate.bible_version_id == bible.id]
    if asset_type:
        conditions.append(AgentAssetCandidate.asset_type == asset_type)
    if review_status:
        conditions.append(AgentAssetCandidate.review_status == review_status)
    result = await db.execute(
        select(AgentAssetCandidate)
        .where(*conditions)
        .order_by(AgentAssetCandidate.asset_type, AgentAssetCandidate.canonical_name)
    )
    return bible.version, list(result.scalars().all())


async def update_asset_candidate(
    db: AsyncSession,
    production_id: UUID,
    candidate_id: UUID,
    user: User,
    payload: AgentAssetCandidateUpdateRequest,
) -> AgentAssetCandidate:
    context = await _get_context(db, production_id, user.id, lock=True, require_bible=True)
    bible = _editable_bible(context)
    result = await db.execute(
        select(AgentAssetCandidate)
        .where(
            AgentAssetCandidate.id == candidate_id,
            AgentAssetCandidate.bible_version_id == bible.id,
        )
        .with_for_update(of=AgentAssetCandidate)
    )
    candidate = result.scalar_one_or_none()
    if candidate is None:
        raise AppException("资产候选不存在", code=40433, status_code=404)
    if candidate.lock_version != payload.expected_lock_version:
        raise AppException(
            f"资产候选版本冲突，当前版本为 {candidate.lock_version}",
            code=40931,
            status_code=409,
            data={
                "expected_lock_version": payload.expected_lock_version,
                "current_lock_version": candidate.lock_version,
            },
        )
    if candidate.materialized_asset_id is not None:
        raise AppException("资产候选已经物化，不能继续修改", code=40925, status_code=409)
    data = payload.model_dump(exclude={"expected_lock_version"}, exclude_none=True)
    if "canonical_name" in data:
        candidate.canonical_name = str(data["canonical_name"]).strip()
    if "aliases" in data:
        candidate.aliases = _clean_names(data["aliases"], exclude=candidate.canonical_name)
    if "content" in data:
        candidate.content = {**(candidate.content or {}), **data["content"]}
    if "review_status" in data:
        candidate.review_status = data["review_status"]
    candidate.lock_version += 1
    candidates_result = await db.execute(
        select(AgentAssetCandidate).where(
            AgentAssetCandidate.bible_version_id == bible.id,
        )
    )
    episode_plan = (context.source_step.extra or {}).get("episode_plan") or {}
    sync_asset_candidate_episode_links(
        list(candidates_result.scalars().all()),
        episode_plan,
    )
    context.source_step.extra = {
        **(context.source_step.extra or {}),
        "episode_plan": episode_plan,
    }
    script_version = bump_script_review_version(
        context.production,
        context.source_step,
        context.checkpoint,
    )
    context.production.lock_version += 1
    db.add(
        AgentEvent(
            production_id=context.production.id,
            step_id=context.story_step.id if context.story_step else None,
            actor_user_id=user.id,
            event_type="asset_candidate.updated",
            source="user",
            payload={
                "candidate_id": str(candidate.id),
                "lock_version": candidate.lock_version,
                "review_status": candidate.review_status,
                "script_version": script_version,
            },
        )
    )
    await db.commit()
    await db.refresh(candidate)
    return candidate


async def materialize_asset_candidates(
    db: AsyncSession,
    production_id: UUID,
    user: User,
    payload: AgentAssetCandidateMaterializeRequest,
) -> Dict[str, Any]:
    context = await _get_context(db, production_id, user.id, lock=True, require_bible=True)
    _reject_legacy_v2_operation(context)
    bible = context.bible
    if bible is None:
        raise AppException("故事圣经尚未生成", code=40924, status_code=409)
    _check_bible_version(bible, payload.expected_bible_version)
    result = await db.execute(
        select(AgentAssetCandidate)
        .where(
            AgentAssetCandidate.bible_version_id == bible.id,
            AgentAssetCandidate.id.in_(payload.candidate_ids),
        )
        .with_for_update(of=AgentAssetCandidate)
    )
    candidates = list(result.scalars().all())
    if len(candidates) != len(payload.candidate_ids):
        raise AppException("部分资产候选不存在", code=40433, status_code=404)
    unresolved = [
        candidate
        for candidate in candidates
        if candidate.review_status not in {"ready", "materialized"}
    ]
    if unresolved:
        raise AppException(
            "存在尚未确认或已拒绝的资产候选，不能物化",
            code=40926,
            status_code=409,
            data={"candidate_ids": [str(candidate.id) for candidate in unresolved]},
        )

    asset_ids, created_count, reused_count = await materialize_candidate_set(
        db,
        context.production,
        candidates,
    )
    context.production.lock_version += 1
    db.add(
        AgentEvent(
            production_id=context.production.id,
            step_id=context.story_step.id if context.story_step else None,
            actor_user_id=user.id,
            event_type="asset_candidates.materialized",
            source="user",
            payload={
                "bible_version": bible.version,
                "candidate_ids": [str(value) for value in payload.candidate_ids],
                "asset_ids": [str(value) for value in asset_ids],
            },
        )
    )
    await db.commit()
    return {
        "bible_version": bible.version,
        "asset_ids": asset_ids,
        "created_count": created_count,
        "reused_count": reused_count,
    }


async def materialize_candidate_set(
    db: AsyncSession,
    production: AgentProduction,
    candidates: List[AgentAssetCandidate],
) -> Tuple[List[UUID], int, int]:
    existing = await _existing_assets(db, production.project_id, production.user_id)
    asset_ids: List[UUID] = []
    created_count = 0
    reused_count = 0
    for candidate in candidates:
        if candidate.materialized_asset_id is not None:
            asset_ids.append(candidate.materialized_asset_id)
            reused_count += 1
            continue
        asset, created = await _materialize_candidate(
            db,
            production,
            candidate,
            existing,
        )
        candidate.materialized_asset_id = asset.id
        candidate.review_status = "materialized"
        candidate.lock_version += 1
        asset_ids.append(asset.id)
        created_count += int(created)
        reused_count += int(not created)
    return asset_ids, created_count, reused_count


def sync_candidate_source_chapters(
    candidates: List[AgentAssetCandidate],
    episode_plan: Dict[str, Any],
    chapters: List[ProjectChapter],
) -> None:
    chapter_by_episode = {
        int((chapter.extra or {}).get("episode_number") or 0): chapter.id
        for chapter in chapters
    }
    for candidate in candidates:
        keys = {
            _normalize_name(value)
            for value in [candidate.canonical_name, *(candidate.aliases or [])]
        }
        candidate.source_chapter_ids = [
            str(value)
            for value in _candidate_source_chapters(
                {"keys": keys},
                candidate.asset_type,
                episode_plan,
                chapter_by_episode,
            )
        ]


def sync_asset_candidate_episode_links(
    candidates: List[AgentAssetCandidate],
    episode_plan: Dict[str, Any],
) -> None:
    episodes = [
        item for item in episode_plan.get("episodes") or [] if isinstance(item, dict)
    ]
    keys = {
        "character": "characters",
        "scene": "scenes",
        "prop": "props",
    }
    original_values = {
        key: [list(episode.get(key) or []) for episode in episodes]
        for key in keys.values()
    }
    for episode in episodes:
        for key in keys.values():
            episode[key] = []

    for candidate in candidates:
        if candidate.review_status == "rejected":
            candidate.content = {
                **(candidate.content or {}),
                "episode_numbers": [],
            }
            continue
        key = keys[candidate.asset_type]
        episode_numbers = _variant_episode_numbers(candidate.content or {}, episode_plan)
        if not episode_numbers:
            candidate_keys = {
                _normalize_name(value)
                for value in [
                    candidate.canonical_name,
                    *(candidate.aliases or []),
                    (candidate.content or {}).get("name"),
                    (candidate.content or {}).get("canonical_name"),
                ]
            }
            candidate_keys.discard("")
            for index, values in enumerate(original_values[key], start=1):
                for value in values:
                    data = value if isinstance(value, dict) else {"name": value}
                    names = {_normalize_name(data.get("name"))}
                    names.update(
                        _normalize_name(alias) for alias in data.get("aliases") or []
                    )
                    if candidate_keys & names:
                        episode_numbers.append(index)
                        break
        episode_numbers = sorted(
            {
                number
                for number in episode_numbers
                if 1 <= number <= len(episodes)
            }
        )
        candidate.content = {
            **(candidate.content or {}),
            "episode_numbers": episode_numbers,
        }
        for episode_number in episode_numbers:
            values = episodes[episode_number - 1][key]
            if candidate.canonical_name not in values:
                values.append(candidate.canonical_name)


async def sync_production_candidate_episode_numbers(
    db: AsyncSession,
    production_id: UUID,
    episode_plan: Dict[str, Any],
) -> None:
    result = await db.execute(
        select(AgentAssetCandidate).where(
            AgentAssetCandidate.production_id == production_id,
        )
    )
    sync_asset_candidate_episode_links(
        list(result.scalars().all()),
        episode_plan,
    )


async def sync_production_variant_episode_numbers(
    db: AsyncSession,
    production_id: UUID,
    episode_plan: Dict[str, Any],
    *,
    recompute_from_evidence: bool = True,
) -> None:
    result = await db.execute(
        select(AgentAssetVariant).where(
            AgentAssetVariant.production_id == production_id,
        )
    )
    sync_asset_variant_episode_links(
        list(result.scalars().all()),
        episode_plan,
        recompute_from_evidence=recompute_from_evidence,
    )


def sync_asset_variant_episode_links(
    variants: List[AgentAssetVariant],
    episode_plan: Dict[str, Any],
    *,
    recompute_from_evidence: bool = True,
) -> None:
    episodes = [
        item for item in episode_plan.get("episodes") or [] if isinstance(item, dict)
    ]
    keys = {
        "character": "character_variants",
        "scene": "scene_variants",
        "prop": "prop_variants",
    }
    for episode in episodes:
        for key in keys.values():
            episode[key] = []
    episode_count = len(episodes)
    for variant in variants:
        if variant.review_status == "rejected":
            continue
        episode_numbers = set()
        if recompute_from_evidence and variant.source_evidence:
            for evidence in variant.source_evidence:
                if isinstance(evidence, dict):
                    episode_numbers.update(_variant_episode_numbers(evidence, episode_plan))
        else:
            episode_numbers.update(
                number
                for number in variant.episode_numbers or []
                if 1 <= _positive_int(number) <= episode_count
            )
        variant.episode_numbers = sorted(episode_numbers)
        key = keys[variant.asset_type]
        for episode_number in variant.episode_numbers:
            values = episodes[episode_number - 1][key]
            if variant.canonical_name not in values:
                values.append(variant.canonical_name)


async def confirm_story_bible(
    db: AsyncSession,
    production_id: UUID,
    user: User,
    payload: SeriesBibleConfirmRequest,
) -> Dict[str, Any]:
    context = await _get_context(db, production_id, user.id, lock=True, require_bible=True)
    _reject_legacy_v2_operation(context)
    bible = context.bible
    if bible is None:
        raise AppException("故事圣经尚未生成", code=40924, status_code=409)
    _check_bible_version(bible, payload.expected_version)
    if bible.status == "confirmed":
        return {
            "bible_version": bible.version,
            "status": bible.status,
            "already_confirmed": True,
            "materialized_asset_count": await _materialized_count(db, bible.id),
        }
    _editable_bible(context)
    unresolved_result = await db.execute(
        select(func.count())
        .select_from(AgentAssetCandidate)
        .where(
            AgentAssetCandidate.bible_version_id == bible.id,
            AgentAssetCandidate.review_status == "needs_review",
        )
    )
    unresolved_count = int(unresolved_result.scalar_one() or 0)
    if unresolved_count:
        raise AppException(
            f"仍有 {unresolved_count} 个低置信度候选需要确认或拒绝",
            code=40927,
            status_code=409,
            data={"needs_review_count": unresolved_count},
        )
    story_step = context.story_step
    checkpoint = context.checkpoint
    if story_step is None or checkpoint is None:
        raise AppException("故事圣经步骤数据不完整", code=50047, status_code=500)
    now = beijing_datetime()
    bible.status = "confirmed"
    bible.confirmed_by = user.id
    bible.confirmed_at = now
    checkpoint.status = transition_checkpoint_status(checkpoint.status, "approved")
    checkpoint.approved_by = user.id
    checkpoint.approved_at = now
    checkpoint.extra = {
        **(checkpoint.extra or {}),
        "confirmed_version": bible.version,
        "confirmation_idempotency_key": payload.idempotency_key,
    }
    story_step.status = transition_step_status(story_step.status, "completed")
    story_step.finished_at = now
    context.production.status = transition_production_status(context.production.status, "planning")
    context.production.current_stage = "core_assets"
    context.production.lock_version += 1
    materialized_count = await _materialized_count(db, bible.id)
    db.add(
        AgentEvent(
            production_id=context.production.id,
            step_id=story_step.id,
            actor_user_id=user.id,
            event_type="story_bible.confirmed",
            source="user",
            payload={
                "bible_version": bible.version,
                "idempotency_key": payload.idempotency_key,
                "materialized_asset_count": materialized_count,
            },
        )
    )
    await db.commit()
    return {
        "bible_version": bible.version,
        "status": bible.status,
        "already_confirmed": False,
        "materialized_asset_count": materialized_count,
    }


def build_asset_candidate_payloads(
    bible_content: Dict[str, Any],
    episode_plan: Dict[str, Any],
    chapters: List[ProjectChapter],
) -> List[Dict[str, Any]]:
    chapter_by_episode = {
        int((chapter.extra or {}).get("episode_number") or 0): chapter.id for chapter in chapters
    }
    payloads: List[Dict[str, Any]] = []
    for asset_type, key in (
        ("character", "characters"),
        ("scene", "scenes"),
        ("prop", "props"),
    ):
        raw_items = bible_content.get(key) or []
        clusters = _merge_candidate_items(raw_items)
        for cluster in clusters:
            source_chapter_ids = _candidate_source_chapters(
                cluster,
                asset_type,
                episode_plan,
                chapter_by_episode,
            )
            candidate_key = hashlib.sha256(
                f"{asset_type}:{'|'.join(sorted(cluster['keys']))}".encode("utf-8")
            ).hexdigest()
            confidence = round(float(cluster["confidence"]), 4)
            canonical_name = str(cluster["canonical_name"])[:128]
            payloads.append(
                {
                    "asset_type": asset_type,
                    "candidate_key": candidate_key,
                    "canonical_name": canonical_name,
                    "aliases": _clean_names(cluster["aliases"], exclude=canonical_name),
                    "source_chapter_ids": [str(value) for value in source_chapter_ids],
                    "confidence": confidence,
                    "merge_reason": cluster["merge_reason"],
                    "review_status": (
                        "needs_review" if confidence < REVIEW_CONFIDENCE_THRESHOLD else "ready"
                    ),
                    "content": _generation_ready_candidate_content(
                        asset_type,
                        cluster["content"],
                    ),
                }
            )
    return payloads


def _generation_ready_candidate_content(
    asset_type: str,
    raw_content: Dict[str, Any],
) -> Dict[str, Any]:
    content = dict(raw_content)
    source_facts = (
        content.get("source_facts")
        if isinstance(content.get("source_facts"), dict)
        else {}
    )
    design_spec = (
        content.get("design_spec")
        if isinstance(content.get("design_spec"), dict)
        else {}
    )

    def fill(field: str, *values: Any) -> None:
        if str(content.get(field) or "").strip():
            return
        text = "，".join(
            value_text
            for value in values
            if (value_text := _asset_design_text(value))
        )
        if text:
            content[field] = text

    fill("description", design_spec.get("design_rationale"), source_facts.get("story_role"))
    if asset_type == "character":
        fill("identity", source_facts.get("story_role"))
        fill("gender", source_facts.get("gender"))
        fill("age", design_spec.get("apparent_age"), source_facts.get("age"))
        fill(
            "appearance",
            design_spec.get("body_type"),
            design_spec.get("face_shape"),
            design_spec.get("facial_features"),
            design_spec.get("hair_style"),
            design_spec.get("hair_color"),
        )
        fill("costume", design_spec.get("default_costume"))
        fill("personality", design_spec.get("temperament"))
    elif asset_type == "scene":
        fill("location", source_facts.get("location"), design_spec.get("location_type"))
        fill("time_of_day", design_spec.get("baseline_time"))
        fill(
            "environment",
            design_spec.get("architecture"),
            design_spec.get("spatial_layout"),
            design_spec.get("key_zones"),
            design_spec.get("materials"),
        )
        fill(
            "atmosphere",
            design_spec.get("baseline_lighting"),
            design_spec.get("color_palette"),
        )
    else:
        fill("category", design_spec.get("category"), source_facts.get("category"))
        fill(
            "appearance",
            design_spec.get("shape"),
            design_spec.get("scale"),
            design_spec.get("material"),
            design_spec.get("color_palette"),
            design_spec.get("markings"),
        )
        fill("function", source_facts.get("function"))

    if source_facts or design_spec:
        prompt_parts = []
        for label, value in (
            ("剧情定位", source_facts.get("story_role")),
            ("视觉设计", design_spec.get("design_rationale")),
            ("身份固定特征", design_spec.get("identity_anchors")),
        ):
            text = _asset_design_text(value)
            if text:
                prompt_parts.append(f"{label}：{text}")
        fill(
            "prompt",
            prompt_parts,
        )
    return content


def _asset_design_text(value: Any) -> str:
    if isinstance(value, list):
        return "、".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def _merge_incremental_bible_content(
    previous: Dict[str, Any],
    analyzed: Dict[str, Any],
) -> Dict[str, Any]:
    merged = {**previous, **analyzed}
    for base_key in ("characters", "scenes", "props"):
        items = [*(previous.get(base_key) or []), *(analyzed.get(base_key) or [])]
        merged[base_key] = [
            {
                **cluster["content"],
                "name": cluster["canonical_name"],
                "aliases": _clean_names(
                    cluster["aliases"],
                    exclude=cluster["canonical_name"],
                ),
                "confidence": cluster["confidence"],
            }
            for cluster in _merge_candidate_items(items)
        ]
    for variant_key in ("character_variants", "scene_variants", "prop_variants"):
        merged[variant_key] = _merge_incremental_variants(
            [*(previous.get(variant_key) or []), *(analyzed.get(variant_key) or [])]
        )
    return _ensure_variant_base_assets(merged)


def _merge_incremental_variants(items: List[Any]) -> List[Dict[str, Any]]:
    merged: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for raw in items:
        if not isinstance(raw, dict):
            continue
        key = (
            _normalize_name(raw.get("base_name") or raw.get("asset_name")),
            _normalize_name(raw.get("variant_type") or raw.get("type")),
            _normalize_name(raw.get("name") or raw.get("variant_name")),
        )
        if not all(key):
            continue
        current = merged.get(key)
        merged[key] = dict(raw) if current is None else _merge_content(current, raw)
    return list(merged.values())


def _content_with_reviewed_assets(
    content: Dict[str, Any],
    candidates: List[AgentAssetCandidate],
    variants: List[AgentAssetVariant],
) -> Dict[str, Any]:
    result = dict(content)
    candidate_by_id = {candidate.id: candidate for candidate in candidates}
    for asset_type, key in (
        ("character", "characters"),
        ("scene", "scenes"),
        ("prop", "props"),
    ):
        reviewed = [
            {
                **(candidate.content or {}),
                "name": candidate.canonical_name,
                "aliases": list(candidate.aliases or []),
                "confidence": candidate.confidence,
            }
            for candidate in candidates
            if candidate.asset_type == asset_type and candidate.review_status != "rejected"
        ]
        result[key] = [*(result.get(key) or []), *reviewed]
    for asset_type, key in (
        ("character", "character_variants"),
        ("scene", "scene_variants"),
        ("prop", "prop_variants"),
    ):
        reviewed_variants = []
        for variant in variants:
            candidate = candidate_by_id.get(variant.base_candidate_id)
            if (
                variant.asset_type != asset_type
                or variant.review_status == "rejected"
                or candidate is None
            ):
                continue
            reviewed_variants.append(
                {
                    **(variant.content or {}),
                    "base_name": candidate.canonical_name,
                    "name": variant.canonical_name,
                    "variant_type": variant.variant_type,
                    "description": variant.description,
                    "trigger_reason": variant.trigger_reason,
                    "episode_numbers": list(variant.episode_numbers or []),
                    "source_evidence": list(variant.source_evidence or []),
                    "confidence": variant.confidence,
                }
            )
        result[key] = [*(result.get(key) or []), *reviewed_variants]
    return result


def _ensure_variant_base_assets(bible_content: Dict[str, Any]) -> Dict[str, Any]:
    content = dict(bible_content)
    for base_key, variant_key in (
        ("characters", "character_variants"),
        ("scenes", "scene_variants"),
        ("props", "prop_variants"),
    ):
        base_items = list(content.get(base_key) or [])
        known_names = set()
        for raw in base_items:
            data = raw if isinstance(raw, dict) else {"name": raw}
            known_names.add(_normalize_name(data.get("name") or data.get("canonical_name")))
            known_names.update(_normalize_name(value) for value in data.get("aliases") or [])
        for raw in content.get(variant_key) or []:
            if not isinstance(raw, dict):
                continue
            base_name = str(raw.get("base_name") or raw.get("asset_name") or "").strip()
            normalized = _normalize_name(base_name)
            if not normalized or normalized in known_names:
                continue
            base_items.append(
                {
                    "name": base_name,
                    "description": "由资产变体关联补全，需人工确认基础设定",
                    "confidence": min(_confidence(raw.get("confidence")), 0.5),
                    "extraction_source": "asset_variant",
                }
            )
            known_names.add(normalized)
        content[base_key] = base_items
    return content


def build_asset_variant_payloads(
    bible_content: Dict[str, Any],
    episode_plan: Dict[str, Any],
    candidates: List[AgentAssetCandidate],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    candidates_by_type = {
        asset_type: [item for item in candidates if item.asset_type == asset_type]
        for asset_type in ASSET_TYPES
    }
    payload_by_key: Dict[Tuple[UUID, str], Dict[str, Any]] = {}
    warnings: List[Dict[str, Any]] = []
    for asset_type, key in (
        ("character", "character_variants"),
        ("scene", "scene_variants"),
        ("prop", "prop_variants"),
    ):
        for raw in bible_content.get(key) or []:
            data = raw if isinstance(raw, dict) else {}
            base_name = str(data.get("base_name") or data.get("asset_name") or "").strip()
            name = str(data.get("name") or data.get("variant_name") or "").strip()
            variant_type = str(data.get("variant_type") or data.get("type") or "").strip()
            base_candidate = _find_base_candidate(candidates_by_type[asset_type], base_name)
            if base_candidate is None or not name or not variant_type:
                warnings.append(
                    {
                        "code": "invalid_asset_variant",
                        "asset_type": asset_type,
                        "base_name": base_name,
                        "name": name,
                        "message": "变体缺少基础资产、名称或类型，需人工处理",
                    }
                )
                continue
            variant_key = hashlib.sha256(
                (
                    f"{asset_type}:{base_candidate.candidate_key}:"
                    f"{_normalize_name(variant_type)}:{_normalize_name(name)}"
                ).encode("utf-8")
            ).hexdigest()
            identity = (base_candidate.id, variant_key)
            confidence = _confidence(data.get("confidence"))
            episode_numbers = _variant_episode_numbers(data, episode_plan)
            source_evidence = _variant_source_evidence(data)
            needs_review = (
                confidence < REVIEW_CONFIDENCE_THRESHOLD
                or variant_type not in AGENT_ASSET_VARIANT_TYPES[asset_type]
                or not str(data.get("description") or "").strip()
                or not str(data.get("trigger_reason") or "").strip()
                or not episode_numbers
                or not source_evidence
            )
            existing = payload_by_key.get(identity)
            if existing is None:
                payload_by_key[identity] = {
                    "base_candidate_id": base_candidate.id,
                    "asset_type": asset_type,
                    "variant_key": variant_key,
                    "canonical_name": name[:128],
                    "variant_type": variant_type[:64],
                    "description": str(data.get("description") or "").strip(),
                    "trigger_reason": str(data.get("trigger_reason") or "").strip(),
                    "episode_numbers": episode_numbers,
                    "source_evidence": source_evidence,
                    "confidence": confidence,
                    "review_status": "needs_review" if needs_review else "ready",
                    "content": dict(data),
                }
                continue
            existing["episode_numbers"] = sorted(
                set(existing["episode_numbers"]) | set(episode_numbers)
            )
            existing["source_evidence"] = _unique_values(
                [*existing["source_evidence"], *source_evidence]
            )
            existing["confidence"] = min(existing["confidence"], confidence)
            if needs_review:
                existing["review_status"] = "needs_review"
            for field in ("description", "trigger_reason"):
                if len(str(data.get(field) or "")) > len(existing[field]):
                    existing[field] = str(data[field]).strip()
            existing["content"] = _merge_content(existing["content"], data)
    return list(payload_by_key.values()), warnings


async def _get_context(
    db: AsyncSession,
    production_id: UUID,
    user_id: UUID,
    *,
    lock: bool,
    require_bible: bool,
) -> StoryBibleContext:
    production = await _get_production(db, production_id, user_id, lock=lock)
    source_result = await db.execute(
        select(AgentStep).where(
            AgentStep.production_id == production.id,
            AgentStep.stage == "source_analysis",
        )
    )
    source_step = source_result.scalar_one_or_none()
    if source_step is None:
        raise AppException("全剧分析步骤不存在", code=40928, status_code=409)
    story_query = select(AgentStep).where(
        AgentStep.production_id == production.id,
        AgentStep.stage == "story_bible",
    )
    if lock:
        story_query = story_query.with_for_update(of=AgentStep)
    story_result = await db.execute(story_query)
    story_step = story_result.scalar_one_or_none()
    checkpoint = None
    if story_step is not None:
        checkpoint_query = select(AgentCheckpoint).where(
            AgentCheckpoint.step_id == story_step.id,
            AgentCheckpoint.checkpoint_type == "story_bible_review",
        )
        if lock:
            checkpoint_query = checkpoint_query.with_for_update(of=AgentCheckpoint)
        checkpoint_result = await db.execute(checkpoint_query)
        checkpoint = checkpoint_result.scalar_one_or_none()
    bible_result = await db.execute(
        select(SeriesBibleVersion)
        .where(SeriesBibleVersion.production_id == production.id)
        .order_by(SeriesBibleVersion.version.desc())
        .limit(1)
    )
    bible = bible_result.scalar_one_or_none()
    if (
        story_step is None
        and bible is not None
        and bible.step_id == source_step.id
        and uses_agent_workflow_v2(production)
    ):
        story_step = source_step
        checkpoint_query = select(AgentCheckpoint).where(
            AgentCheckpoint.step_id == story_step.id,
            AgentCheckpoint.checkpoint_type == "script_review",
        )
        if lock:
            checkpoint_query = checkpoint_query.with_for_update(of=AgentCheckpoint)
        checkpoint_result = await db.execute(checkpoint_query)
        checkpoint = checkpoint_result.scalar_one_or_none()
    if require_bible and (story_step is None or checkpoint is None or bible is None):
        raise AppException("故事圣经尚未生成", code=40924, status_code=409)
    return StoryBibleContext(production, source_step, story_step, checkpoint, bible)


async def _get_production(
    db: AsyncSession,
    production_id: UUID,
    user_id: UUID,
    *,
    lock: bool,
) -> AgentProduction:
    query = (
        select(AgentProduction)
        .join(Project, Project.id == AgentProduction.project_id)
        .where(
            AgentProduction.id == production_id,
            AgentProduction.user_id == user_id,
            Project.user_id == user_id,
            Project.is_enabled.is_(True),
        )
    )
    if lock:
        query = query.with_for_update(of=AgentProduction)
    result = await db.execute(query)
    production = result.scalar_one_or_none()
    if production is None:
        raise AppException("整剧任务不存在", code=40430, status_code=404)
    return production


async def _build_bible_content(
    db: AsyncSession,
    context: StoryBibleContext,
) -> Dict[str, Any]:
    analysis = (context.source_step.extra or {}).get("global_analysis") or {}
    return await _build_bible_content_from_analysis(db, context.production, analysis)


async def _build_bible_content_from_analysis(
    db: AsyncSession,
    production: AgentProduction,
    analysis: Dict[str, Any],
) -> Dict[str, Any]:
    project = await db.get(Project, production.project_id)
    style = await db.get(Style, project.style_id) if project is not None else None
    return {
        "story_summary": analysis.get("story_summary") or "",
        "worldview": analysis.get("worldview") or "",
        "genre": analysis.get("genre") or [],
        "main_plot": analysis.get("main_plot") or "",
        "subplots": analysis.get("subplots") or [],
        "timeline": analysis.get("timeline") or [],
        "character_relationships": analysis.get("character_relationships")
        or analysis.get("relationships")
        or [],
        "characters": analysis.get("characters") or [],
        "character_variants": analysis.get("character_variants") or [],
        "scenes": analysis.get("scenes") or [],
        "scene_variants": analysis.get("scene_variants") or [],
        "props": analysis.get("props") or [],
        "prop_variants": analysis.get("prop_variants") or [],
        "visual_style": {
            "style_id": str(project.style_id) if project is not None else None,
            "name": style.name if style is not None else "",
            "prompt": style.prompt if style is not None else "",
            "generation_ratio": project.generation_ratio if project is not None else "",
        },
        "continuity_rules": analysis.get("continuity_rules") or [],
        "forbidden_conflicts": analysis.get("forbidden_conflicts") or [],
    }


async def _production_chapters(
    db: AsyncSession,
    production: AgentProduction,
) -> List[ProjectChapter]:
    result = await db.execute(
        select(ProjectChapter).where(
            ProjectChapter.project_id == production.project_id,
            ProjectChapter.user_id == production.user_id,
            ProjectChapter.is_enabled.is_(True),
            ProjectChapter.extra["agent_production_id"].as_string() == str(production.id),
        )
    )
    return list(result.scalars().all())


def _merge_candidate_items(raw_items: Iterable[Any]) -> List[Dict[str, Any]]:
    clusters: List[Dict[str, Any]] = []
    for raw in raw_items:
        item = _candidate_item(raw)
        if item is None:
            continue
        best_index = None
        best_score = 0.0
        best_reason = ""
        for index, cluster in enumerate(clusters):
            score, reason = _candidate_match(cluster, item)
            if score > best_score:
                best_index, best_score, best_reason = index, score, reason
        if best_index is None or best_score <= 0:
            clusters.append(
                {
                    "canonical_name": item["name"],
                    "aliases": list(item["aliases"]),
                    "keys": set(item["keys"]),
                    "confidence": item["confidence"],
                    "merge_reason": "单一候选，无跨集合并",
                    "content": item["content"],
                }
            )
            continue
        cluster = clusters[best_index]
        cluster["aliases"] = _clean_names(
            [*cluster["aliases"], item["name"], *item["aliases"]],
            exclude=cluster["canonical_name"],
        )
        cluster["keys"].update(item["keys"])
        cluster["confidence"] = min(cluster["confidence"], item["confidence"], best_score)
        cluster["merge_reason"] = best_reason
        cluster["content"] = _merge_content(cluster["content"], item["content"])
    return clusters


def _candidate_item(raw: Any) -> Optional[Dict[str, Any]]:
    data = raw if isinstance(raw, dict) else {"name": raw}
    name = str(data.get("name") or data.get("canonical_name") or "").strip()
    if not name:
        return None
    aliases = _clean_names(data.get("aliases") or [], exclude=name)
    keys = {_normalize_name(value) for value in [name, *aliases]}
    keys.discard("")
    try:
        confidence = float(data.get("confidence", 1.0))
    except (TypeError, ValueError):
        confidence = 1.0
    return {
        "name": name,
        "aliases": aliases,
        "keys": keys,
        "confidence": max(0.0, min(confidence, 1.0)),
        "content": dict(data),
    }


def _candidate_match(cluster: Dict[str, Any], item: Dict[str, Any]) -> Tuple[float, str]:
    cluster_name = _normalize_name(cluster["canonical_name"])
    item_name = _normalize_name(item["name"])
    if cluster_name == item_name:
        return 0.98, "标准名称一致"
    cluster_aliases = {_normalize_name(value) for value in cluster["aliases"]} - _WEAK_ALIASES
    item_aliases = {_normalize_name(value) for value in item["aliases"]} - _WEAK_ALIASES
    if item_name in cluster_aliases or cluster_name in item_aliases:
        return 0.93, "标准名称与别名匹配"
    if cluster_aliases & item_aliases:
        return 0.7, "仅别名相交，需人工确认"
    return 0.0, ""


def _candidate_source_chapters(
    cluster: Dict[str, Any],
    asset_type: str,
    episode_plan: Dict[str, Any],
    chapter_by_episode: Dict[int, UUID],
) -> List[UUID]:
    keys = set(cluster["keys"])
    result: List[UUID] = []
    episode_key = {"character": "characters", "scene": "scenes", "prop": "props"}[asset_type]
    for episode in episode_plan.get("episodes") or []:
        if not isinstance(episode, dict):
            continue
        entity_keys = set()
        for value in episode.get(episode_key) or []:
            item = value if isinstance(value, dict) else {"name": value}
            entity_keys.add(_normalize_name(item.get("name")))
            entity_keys.update(_normalize_name(alias) for alias in item.get("aliases") or [])
        if keys & entity_keys:
            chapter_id = chapter_by_episode.get(int(episode.get("episode_number") or 0))
            if chapter_id is not None and chapter_id not in result:
                result.append(chapter_id)
    return result


def _find_base_candidate(
    candidates: List[AgentAssetCandidate],
    base_name: str,
) -> Optional[AgentAssetCandidate]:
    key = _normalize_name(base_name)
    if not key:
        return None
    for candidate in candidates:
        names = [candidate.canonical_name, *(candidate.aliases or [])]
        if key in {_normalize_name(name) for name in names}:
            return candidate
    return None


def _variant_episode_numbers(
    variant: Dict[str, Any],
    episode_plan: Dict[str, Any],
) -> List[int]:
    explicit = variant.get("episode_numbers")
    if not isinstance(explicit, list):
        explicit = [variant.get("episode_number")] if variant.get("episode_number") else []
    result = {
        number
        for value in explicit
        if (number := _positive_int(value)) > 0
    }
    evidence = variant.get("source_evidence")
    ranges = (
        [item for item in evidence if isinstance(item, dict)]
        if isinstance(evidence, list)
        else []
    )
    ranges.append(variant)
    for item in ranges:
        try:
            source_start = int(item.get("source_start"))
            source_end = int(item.get("source_end"))
        except (TypeError, ValueError):
            continue
        if source_end <= source_start or source_start < 0:
            continue
        for index, episode in enumerate(episode_plan.get("episodes") or [], start=1):
            if not isinstance(episode, dict):
                continue
            try:
                episode_start = int(episode.get("source_start"))
                episode_end = int(episode.get("source_end"))
            except (TypeError, ValueError):
                continue
            if source_start < episode_end and source_end > episode_start:
                result.add(_positive_int(episode.get("episode_number")) or index)
    return sorted(result)


def _variant_source_evidence(variant: Dict[str, Any]) -> List[Dict[str, Any]]:
    evidence = variant.get("source_evidence")
    if isinstance(evidence, list):
        normalized = [dict(item) for item in evidence if isinstance(item, dict)]
        if normalized:
            return normalized
    try:
        source_start = int(variant.get("source_start"))
        source_end = int(variant.get("source_end"))
    except (TypeError, ValueError):
        return []
    if source_end <= source_start or source_start < 0:
        return []
    return [{"source_start": source_start, "source_end": source_end}]


def _confidence(value: Any) -> float:
    try:
        confidence = float(value if value is not None else 1.0)
    except (TypeError, ValueError):
        confidence = 1.0
    return round(max(0.0, min(confidence, 1.0)), 4)


def _positive_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _merge_content(first: Dict[str, Any], second: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(first)
    for key, value in second.items():
        if key in {"name", "canonical_name", "aliases", "confidence"}:
            continue
        current = result.get(key)
        if isinstance(current, list) or isinstance(value, list):
            result[key] = _unique_values(
                list(current if isinstance(current, list) else [])
                + list(value if isinstance(value, list) else [])
            )
        elif len(str(value or "")) > len(str(current or "")):
            result[key] = value
    return result


def _clean_names(values: Iterable[Any], *, exclude: str) -> List[str]:
    exclude_key = _normalize_name(exclude)
    result: List[str] = []
    seen = {exclude_key}
    for value in values:
        name = str(value or "").strip()
        key = _normalize_name(name)
        if name and key and key not in seen:
            seen.add(key)
            result.append(name[:128])
    return result


def _normalize_name(value: Any) -> str:
    return re.sub(r"[\W_]+", "", str(value or "").strip().lower(), flags=re.UNICODE)


def _unique_values(values: Iterable[Any]) -> List[Any]:
    result = []
    seen = set()
    for value in values:
        key = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _editable_bible(context: StoryBibleContext) -> SeriesBibleVersion:
    bible = context.bible
    if bible is None or context.story_step is None or context.checkpoint is None:
        raise AppException("故事圣经尚未生成", code=40924, status_code=409)
    if (
        bible.status != "draft"
        or context.production.status != "waiting_approval"
        or context.story_step.status != "waiting_approval"
        or context.checkpoint.status != "pending"
    ):
        raise AppException("当前状态不允许修改故事圣经", code=40929, status_code=409)
    return bible


def _reject_legacy_v2_operation(context: StoryBibleContext) -> None:
    if (
        uses_agent_workflow_v2(context.production)
        and context.checkpoint is not None
        and context.checkpoint.checkpoint_type == "script_review"
    ):
        raise AppException(
            "新流程请通过剧本处理包审核并确认",
            code=40947,
            status_code=409,
        )


def _check_bible_version(bible: SeriesBibleVersion, expected_version: int) -> None:
    if bible.version != expected_version:
        raise AppException(
            f"故事圣经版本冲突，当前版本为 {bible.version}",
            code=40930,
            status_code=409,
            data={"expected_version": expected_version, "current_version": bible.version},
        )


async def _existing_assets(
    db: AsyncSession,
    project_id: UUID,
    user_id: UUID,
) -> Dict[str, List[Any]]:
    result: Dict[str, List[Any]] = {}
    for asset_type, model in (
        ("character", ProjectCharacter),
        ("scene", ProjectScene),
        ("prop", ProjectProp),
    ):
        query_result = await db.execute(
            select(model).where(
                model.project_id == project_id,
                model.user_id == user_id,
                model.is_enabled.is_(True),
            )
        )
        result[asset_type] = list(query_result.scalars().all())
    return result


async def _materialize_candidate(
    db: AsyncSession,
    production: AgentProduction,
    candidate: AgentAssetCandidate,
    existing: Dict[str, List[Any]],
) -> Tuple[Any, bool]:
    asset = _find_existing_asset(existing[candidate.asset_type], candidate)
    created = asset is None
    if asset is None:
        asset = _new_asset(production, candidate)
        db.add(asset)
        await db.flush()
        existing[candidate.asset_type].append(asset)
    _merge_candidate_into_asset(asset, candidate)
    return asset, created


def _find_existing_asset(assets: List[Any], candidate: AgentAssetCandidate) -> Optional[Any]:
    candidate_name = _normalize_name(candidate.canonical_name)
    candidate_aliases = {
        _normalize_name(value) for value in candidate.aliases or []
    } - _WEAK_ALIASES
    for asset in assets:
        aliases = (
            asset.aliases
            if isinstance(asset, ProjectCharacter)
            else (asset.extra or {}).get("aliases") or []
        )
        asset_name = _normalize_name(asset.name)
        asset_aliases = {_normalize_name(value) for value in aliases} - _WEAK_ALIASES
        if (
            candidate_name == asset_name
            or candidate_name in asset_aliases
            or asset_name in candidate_aliases
        ):
            return asset
    return None


def _new_asset(production: AgentProduction, candidate: AgentAssetCandidate) -> Any:
    common = {
        "project_id": production.project_id,
        "user_id": production.user_id,
        "source_chapter_id": _first_uuid(candidate.source_chapter_ids),
        "name": candidate.canonical_name,
        "description": _content_text(candidate.content, "description"),
        "prompt": _content_text(candidate.content, "prompt"),
        "source_content": _content_text(candidate.content, "source_content"),
        "extra": {},
        "is_enabled": True,
    }
    content = candidate.content or {}
    if candidate.asset_type == "character":
        return ProjectCharacter(
            **common,
            aliases=list(candidate.aliases or []),
            identity=_content_text(content, "identity", 128),
            gender=_content_text(content, "gender", 32),
            age=_content_text(content, "age", 64),
            appearance=_content_text(content, "appearance"),
            personality=_content_text(content, "personality"),
            relationship=_content_text(content, "relationship", fallback_key="relationships"),
            costume=_content_text(content, "costume"),
        )
    if candidate.asset_type == "scene":
        return ProjectScene(
            **common,
            location=_content_text(content, "location", 255),
            time_of_day=_content_text(content, "time_of_day", 64, fallback_key="time"),
            environment=_content_text(content, "environment"),
            atmosphere=_content_text(content, "atmosphere"),
        )
    return ProjectProp(
        **common,
        category=_content_text(content, "category", 64, fallback_key="prop_type"),
        appearance=_content_text(content, "appearance"),
        function=_content_text(content, "function"),
    )


def _merge_candidate_into_asset(asset: Any, candidate: AgentAssetCandidate) -> None:
    extra = asset.extra or {}
    candidate_ids = _unique_values([*(extra.get("agent_candidate_ids") or []), str(candidate.id)])
    aliases = list(candidate.aliases or [])
    if _normalize_name(asset.name) != _normalize_name(candidate.canonical_name):
        aliases.append(candidate.canonical_name)
    if isinstance(asset, ProjectCharacter):
        asset.aliases = _clean_names([*(asset.aliases or []), *aliases], exclude=asset.name)
    else:
        extra["aliases"] = _clean_names(
            [*(extra.get("aliases") or []), *aliases],
            exclude=asset.name,
        )
    if asset.source_chapter_id is None:
        asset.source_chapter_id = _first_uuid(candidate.source_chapter_ids)
    _fill_blank_asset_fields(asset, candidate.content or {})
    asset.extra = {
        **extra,
        "agent_production_id": str(candidate.production_id),
        "agent_candidate_ids": candidate_ids,
        "candidate_confidence": candidate.confidence,
        "candidate_merge_reason": candidate.merge_reason,
        "character_states": (
            (candidate.content or {}).get("states") or []
            if isinstance(asset, ProjectCharacter)
            else extra.get("character_states") or []
        ),
    }


def _fill_blank_asset_fields(asset: Any, content: Dict[str, Any]) -> None:
    field_specs = [
        ("description", "description", 0, None),
        ("prompt", "prompt", 0, None),
        ("source_content", "source_content", 0, None),
    ]
    if isinstance(asset, ProjectCharacter):
        field_specs.extend(
            [
                ("identity", "identity", 128, None),
                ("gender", "gender", 32, None),
                ("age", "age", 64, None),
                ("appearance", "appearance", 0, None),
                ("personality", "personality", 0, None),
                ("relationship", "relationship", 0, "relationships"),
                ("costume", "costume", 0, None),
            ]
        )
    elif isinstance(asset, ProjectScene):
        field_specs.extend(
            [
                ("location", "location", 255, None),
                ("time_of_day", "time_of_day", 64, "time"),
                ("environment", "environment", 0, None),
                ("atmosphere", "atmosphere", 0, None),
            ]
        )
    else:
        field_specs.extend(
            [
                ("category", "category", 64, "prop_type"),
                ("appearance", "appearance", 0, None),
                ("function", "function", 0, None),
            ]
        )
    for attribute, key, max_length, fallback_key in field_specs:
        if getattr(asset, attribute) in (None, ""):
            value = _content_text(
                content,
                key,
                max_length,
                fallback_key=fallback_key,
            )
            if value is not None:
                setattr(asset, attribute, value)


def _content_text(
    content: Dict[str, Any],
    key: str,
    max_length: int = 0,
    *,
    fallback_key: Optional[str] = None,
) -> Optional[str]:
    value = content.get(key)
    if value in (None, "") and fallback_key:
        value = content.get(fallback_key)
    if isinstance(value, list):
        value = "；".join(str(item) for item in value if item not in (None, ""))
    if value in (None, ""):
        return None
    text = str(value)
    return text[:max_length] if max_length else text


def _first_uuid(values: Iterable[Any]) -> Optional[UUID]:
    for value in values:
        try:
            return UUID(str(value))
        except (TypeError, ValueError):
            continue
    return None


async def _materialized_count(db: AsyncSession, bible_version_id: UUID) -> int:
    result = await db.execute(
        select(func.count())
        .select_from(AgentAssetCandidate)
        .where(
            AgentAssetCandidate.bible_version_id == bible_version_id,
            AgentAssetCandidate.materialized_asset_id.is_not(None),
        )
    )
    return int(result.scalar_one() or 0)
