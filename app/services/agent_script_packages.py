from typing import Any, Dict, List
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException
from app.core.timezone import beijing_datetime
from app.models.agent_production import AgentEvent
from app.models.agent_story_bible import (
    AGENT_ASSET_VARIANT_TYPES,
    AgentAssetCandidate,
    AgentAssetVariant,
    SeriesBibleVersion,
)
from app.models.project_chapter import ProjectChapter
from app.models.user import User
from app.schemas.agent_script_package import AgentScriptPackageConfirmRequest
from app.schemas.agent_story_bible import AgentAssetVariantUpdateRequest
from app.services.agent_episode_plans import (
    confirmable_episode_plan_items,
    episode_plan_document,
    load_episode_plan_context,
    materialize_episode_chapters,
)
from app.services.agent_production_state import (
    transition_checkpoint_status,
    transition_production_status,
    transition_step_status,
)
from app.services.agent_story_bibles import (
    materialize_candidate_set,
    sync_candidate_source_chapters,
    sync_production_variant_episode_numbers,
)
from app.services.agent_workflow import bump_script_review_version, uses_agent_workflow_v2


async def get_script_package(
    db: AsyncSession,
    production_id: UUID,
    user_id: UUID,
) -> Dict[str, Any]:
    context = await load_episode_plan_context(db, production_id, user_id, lock=False)
    _require_script_package(context)
    bible = await _latest_bible(db, production_id, lock=False)
    candidates = await _candidates(db, bible.id, lock=False)
    variants = await _variants(db, bible.id, lock=False)
    document = episode_plan_document(context)
    return {
        "production_id": production_id,
        "script_version": document["version"],
        "bible_version": bible.version,
        "status": (
            "confirmed" if context.checkpoint.status == "approved" else "waiting_review"
        ),
        "episodes": document["items"],
        "characters": _by_type(candidates, "character"),
        "character_variants": _by_type(variants, "character"),
        "scenes": _by_type(candidates, "scene"),
        "scene_variants": _by_type(variants, "scene"),
        "props": _by_type(candidates, "prop"),
        "prop_variants": _by_type(variants, "prop"),
        "warnings": list((context.step.extra or {}).get("script_warnings") or []),
    }


async def update_script_asset_variant(
    db: AsyncSession,
    production_id: UUID,
    variant_id: UUID,
    user: User,
    payload: AgentAssetVariantUpdateRequest,
) -> AgentAssetVariant:
    context = await load_episode_plan_context(db, production_id, user.id, lock=True)
    _require_editable_script_package(context)
    bible = await _latest_bible(db, production_id, lock=True)
    result = await db.execute(
        select(AgentAssetVariant)
        .where(
            AgentAssetVariant.id == variant_id,
            AgentAssetVariant.bible_version_id == bible.id,
        )
        .with_for_update(of=AgentAssetVariant)
    )
    variant = result.scalar_one_or_none()
    if variant is None:
        raise AppException("资产变体不存在", code=40440, status_code=404)
    if variant.lock_version != payload.expected_lock_version:
        raise AppException(
            f"资产变体版本冲突，当前版本为 {variant.lock_version}",
            code=40940,
            status_code=409,
            data={
                "expected_lock_version": payload.expected_lock_version,
                "current_lock_version": variant.lock_version,
            },
        )
    update = payload.model_dump(exclude={"expected_lock_version"}, exclude_none=True)
    if (
        "variant_type" in update
        and str(update["variant_type"]).strip().lower()
        not in AGENT_ASSET_VARIANT_TYPES[variant.asset_type]
    ):
        raise AppException("资产变体类型无效", code=40061, status_code=400)
    if "episode_numbers" in update:
        episode_count = len(episode_plan_document(context)["items"])
        if any(number > episode_count for number in update["episode_numbers"]):
            raise AppException("资产变体关联集数超出当前分集范围", code=40060, status_code=400)
    if "source_evidence" in update:
        source_length = len(context.source.content)
        if any(item["source_end"] > source_length for item in update["source_evidence"]):
            raise AppException("资产变体原文证据超出剧本范围", code=40062, status_code=400)
    for field, value in update.items():
        if field in {"canonical_name", "variant_type"}:
            value = str(value).strip().lower() if field == "variant_type" else str(value).strip()
        if field == "content":
            value = {**(variant.content or {}), **value}
        setattr(variant, field, value)
    variant.lock_version += 1
    episode_plan = (context.step.extra or {}).get("episode_plan") or {}
    await sync_production_variant_episode_numbers(
        db,
        production_id,
        episode_plan,
        recompute_from_evidence=(
            "source_evidence" in update and "episode_numbers" not in update
        ),
    )
    context.step.extra = {
        **(context.step.extra or {}),
        "episode_plan": episode_plan,
    }
    script_version = bump_script_review_version(
        context.production,
        context.step,
        context.checkpoint,
    )
    context.production.lock_version += 1
    db.add(
        AgentEvent(
            production_id=production_id,
            step_id=context.step.id,
            actor_user_id=user.id,
            event_type="asset_variant.updated",
            source="user",
            payload={
                "variant_id": str(variant.id),
                "lock_version": variant.lock_version,
                "review_status": variant.review_status,
                "script_version": script_version,
            },
        )
    )
    await db.commit()
    await db.refresh(variant)
    return variant


async def confirm_script_package(
    db: AsyncSession,
    production_id: UUID,
    user: User,
    payload: AgentScriptPackageConfirmRequest,
) -> Dict[str, Any]:
    context = await load_episode_plan_context(db, production_id, user.id, lock=True)
    _require_script_package(context)
    script_version = max(1, int(context.step.output_version or 1))
    if script_version != payload.expected_script_version:
        raise AppException(
            f"剧本处理版本冲突，当前版本为 {script_version}",
            code=40941,
            status_code=409,
            data={"current_script_version": script_version},
        )
    bible = await _latest_bible(db, production_id, lock=True)
    if bible.version != payload.expected_bible_version:
        raise AppException(
            f"资产版本冲突，当前版本为 {bible.version}",
            code=40942,
            status_code=409,
            data={"current_bible_version": bible.version},
        )
    if context.checkpoint.status == "approved":
        extra = context.checkpoint.extra or {}
        stored_key = str(extra.get("confirmation_idempotency_key") or "")
        if stored_key and stored_key != payload.idempotency_key:
            raise AppException(
                "该版本已经使用其他幂等键确认",
                code=40948,
                status_code=409,
            )
        return {
            "production_id": production_id,
            "script_version": script_version,
            "bible_version": bible.version,
            "chapter_ids": _uuid_list(extra.get("materialized_chapter_ids")),
            "asset_ids": _uuid_list(extra.get("materialized_asset_ids")),
            "created_chapter_count": 0,
            "created_asset_count": 0,
            "reused_asset_count": len(_uuid_list(extra.get("materialized_asset_ids"))),
            "already_confirmed": True,
        }
    _require_editable_script_package(context)
    candidates = await _candidates(db, bible.id, lock=True)
    variants = await _variants(db, bible.id, lock=True)
    script_warnings = list((context.step.extra or {}).get("script_warnings") or [])
    if script_warnings:
        raise AppException(
            "剧本处理结果仍有未解决的结构警告",
            code=40949,
            status_code=409,
            data={"warnings": script_warnings},
        )
    unresolved_candidates = [
        item for item in candidates if item.review_status == "needs_review"
    ]
    unresolved_variants = [
        item for item in variants if item.review_status == "needs_review"
    ]
    for item in [*unresolved_candidates, *unresolved_variants]:
        item.review_status = "ready"
        item.updated_at = beijing_datetime()
    active_candidate_ids = {
        item.id
        for item in candidates
        if item.review_status in {"ready", "materialized"}
    }
    orphan_variants = [
        item
        for item in variants
        if item.review_status != "rejected"
        and item.base_candidate_id not in active_candidate_ids
    ]
    if orphan_variants:
        raise AppException(
            "存在基础资产已拒绝但仍保留的资产变体",
            code=40943,
            status_code=409,
            data={"variant_ids": [str(item.id) for item in orphan_variants]},
        )
    invalid_variants = _invalid_confirmable_variants(
        variants,
        episode_count=len(episode_plan_document(context)["items"]),
        source_length=len(context.source.content),
    )
    if invalid_variants:
        raise AppException(
            "存在缺少类型、触发原因、关联集数或原文证据的资产变体",
            code=40943,
            status_code=409,
            data={"variant_ids": [str(item.id) for item in invalid_variants]},
        )

    items = confirmable_episode_plan_items(context)
    (
        chapter_ids,
        created_chapter_count,
        changed_chapter_ids,
    ) = await materialize_episode_chapters(
        db,
        context,
        user.id,
        items,
        script_version,
    )
    chapter_result = await db.execute(
        select(ProjectChapter).where(ProjectChapter.id.in_(chapter_ids))
    )
    chapters = list(chapter_result.scalars().all())
    episode_plan = (context.step.extra or {}).get("episode_plan") or {}
    active_candidates = [
        item for item in candidates if item.review_status in {"ready", "materialized"}
    ]
    sync_candidate_source_chapters(active_candidates, episode_plan, chapters)
    asset_ids, created_asset_count, reused_asset_count = await materialize_candidate_set(
        db,
        context.production,
        active_candidates,
    )

    now = beijing_datetime()
    bible.status = "confirmed"
    bible.confirmed_by = user.id
    bible.confirmed_at = now
    context.checkpoint.status = transition_checkpoint_status(
        context.checkpoint.status,
        "approved",
    )
    context.checkpoint.approved_by = user.id
    context.checkpoint.approved_at = now
    context.checkpoint.extra = {
        **(context.checkpoint.extra or {}),
        "confirmed_script_version": script_version,
        "confirmed_bible_version": bible.version,
        "confirmation_idempotency_key": payload.idempotency_key,
        "materialized_chapter_ids": [str(value) for value in chapter_ids],
        "materialized_asset_ids": [str(value) for value in asset_ids],
    }
    context.step.status = transition_step_status(context.step.status, "completed")
    context.step.finished_at = now
    context.production.status = transition_production_status(
        context.production.status,
        "planning",
    )
    context.production.current_stage = "core_assets"
    production_extra = dict(context.production.extra or {})
    if (context.step.extra or {}).get("incremental"):
        pending_ids = {
            str(value)
            for value in production_extra.get(
                "pending_incremental_storyboard_chapter_ids",
                [],
            )
        }
        pending_ids.update(str(value) for value in changed_chapter_ids)
        production_extra["pending_incremental_storyboard_chapter_ids"] = sorted(
            pending_ids
        )
        production_extra.pop("pending_incremental_storyboard_episode_numbers", None)
    else:
        production_extra.pop("pending_incremental_storyboard_chapter_ids", None)
        production_extra.pop("pending_incremental_storyboard_episode_numbers", None)
    context.production.extra = production_extra
    context.production.lock_version += 1
    db.add(
        AgentEvent(
            production_id=production_id,
            step_id=context.step.id,
            actor_user_id=user.id,
            event_type="script_package.confirmed",
            source="user",
            payload={
                "script_version": script_version,
                "bible_version": bible.version,
                "chapter_ids": [str(value) for value in chapter_ids],
                "asset_ids": [str(value) for value in asset_ids],
                "variant_count": len(
                    [item for item in variants if item.review_status != "rejected"]
                ),
                "idempotency_key": payload.idempotency_key,
            },
        )
    )
    await db.commit()
    return {
        "production_id": production_id,
        "script_version": script_version,
        "bible_version": bible.version,
        "chapter_ids": chapter_ids,
        "asset_ids": asset_ids,
        "created_chapter_count": created_chapter_count,
        "created_asset_count": created_asset_count,
        "reused_asset_count": reused_asset_count,
        "already_confirmed": False,
    }


async def _latest_bible(
    db: AsyncSession,
    production_id: UUID,
    *,
    lock: bool,
) -> SeriesBibleVersion:
    query = (
        select(SeriesBibleVersion)
        .where(SeriesBibleVersion.production_id == production_id)
        .order_by(SeriesBibleVersion.version.desc())
        .limit(1)
    )
    if lock:
        query = query.with_for_update(of=SeriesBibleVersion)
    result = await db.execute(query)
    bible = result.scalar_one_or_none()
    if bible is None:
        raise AppException("剧本资产尚未生成", code=40944, status_code=409)
    return bible


async def _candidates(
    db: AsyncSession,
    bible_id: UUID,
    *,
    lock: bool,
) -> List[AgentAssetCandidate]:
    query = select(AgentAssetCandidate).where(
        AgentAssetCandidate.bible_version_id == bible_id
    )
    if lock:
        query = query.with_for_update(of=AgentAssetCandidate)
    result = await db.execute(query)
    return list(result.scalars().all())


async def _variants(
    db: AsyncSession,
    bible_id: UUID,
    *,
    lock: bool,
) -> List[AgentAssetVariant]:
    query = select(AgentAssetVariant).where(
        AgentAssetVariant.bible_version_id == bible_id
    )
    if lock:
        query = query.with_for_update(of=AgentAssetVariant)
    result = await db.execute(query)
    return list(result.scalars().all())


def _require_script_package(context: Any) -> None:
    if (
        not uses_agent_workflow_v2(context.production)
        or context.checkpoint.checkpoint_type != "script_review"
    ):
        raise AppException("当前任务不使用统一剧本处理流程", code=40945, status_code=409)


def _require_editable_script_package(context: Any) -> None:
    _require_script_package(context)
    if (
        context.production.status != "waiting_approval"
        or context.step.status != "waiting_approval"
        or context.checkpoint.status != "pending"
    ):
        raise AppException("当前状态不允许修改或确认剧本处理结果", code=40946, status_code=409)


def _by_type(items: List[Any], asset_type: str) -> List[Any]:
    return [item for item in items if item.asset_type == asset_type]


def _uuid_list(values: Any) -> List[UUID]:
    result = []
    for value in values or []:
        try:
            result.append(UUID(str(value)))
        except (TypeError, ValueError):
            continue
    return result


def _invalid_confirmable_variants(
    variants: List[AgentAssetVariant],
    *,
    episode_count: int,
    source_length: int,
) -> List[AgentAssetVariant]:
    invalid = []
    for variant in variants:
        if variant.review_status == "rejected":
            continue
        evidence = variant.source_evidence or []
        evidence_valid = bool(evidence) and all(
            _evidence_in_range(item, source_length) for item in evidence
        )
        episodes_valid = bool(variant.episode_numbers) and all(
            _episode_in_range(number, episode_count) for number in variant.episode_numbers
        )
        if (
            variant.variant_type not in AGENT_ASSET_VARIANT_TYPES[variant.asset_type]
            or not variant.description.strip()
            or not variant.trigger_reason.strip()
            or not evidence_valid
            or not episodes_valid
        ):
            invalid.append(variant)
    return invalid


def _evidence_in_range(value: Any, source_length: int) -> bool:
    if not isinstance(value, dict):
        return False
    try:
        return (
            0
            <= int(value.get("source_start"))
            < int(value.get("source_end"))
            <= source_length
        )
    except (TypeError, ValueError):
        return False


def _episode_in_range(value: Any, episode_count: int) -> bool:
    try:
        return 1 <= int(value) <= episode_count
    except (TypeError, ValueError):
        return False
