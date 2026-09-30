import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException
from app.core.timezone import beijing_datetime
from app.models.agent_production import (
    AgentCheckpoint,
    AgentEvent,
    AgentProduction,
    AgentStep,
    ProjectSourceDocument,
)
from app.models.project import Project
from app.models.project_chapter import ProjectChapter
from app.models.user import User
from app.schemas.agent_production import (
    AgentEpisodePlanConfirmRequest,
    AgentEpisodePlanMergeRequest,
    AgentEpisodePlanSplitRequest,
    AgentEpisodePlanUpdateRequest,
)
from app.services.agent.production_state import (
    transition_checkpoint_status,
    transition_production_status,
    transition_step_status,
)
from app.services.agent.workflow_steps import initialize_agent_episode_workflow_states
from app.services.agent.source_text import validate_agent_stage_output
from app.services.agent.story_bibles import (
    sync_production_candidate_episode_numbers,
    sync_production_variant_episode_numbers,
)
from app.services.agent.workflow import uses_agent_workflow_v2


@dataclass
class EpisodePlanContext:
    production: AgentProduction
    step: AgentStep
    checkpoint: AgentCheckpoint
    source: ProjectSourceDocument


async def get_episode_plan_document(
    db: AsyncSession,
    production_id: UUID,
    user_id: UUID,
) -> Dict[str, Any]:
    context = await _get_context(db, production_id, user_id, lock=False)
    return _document(context)


async def load_episode_plan_context(
    db: AsyncSession,
    production_id: UUID,
    user_id: UUID,
    *,
    lock: bool,
) -> EpisodePlanContext:
    return await _get_context(db, production_id, user_id, lock=lock)


def confirmable_episode_plan_items(
    context: EpisodePlanContext,
) -> List[Dict[str, Any]]:
    return _validate_confirmable_items(context)


def episode_plan_document(context: EpisodePlanContext) -> Dict[str, Any]:
    return _document(context)


async def update_episode_plan(
    db: AsyncSession,
    production_id: UUID,
    plan_id: UUID,
    user: User,
    payload: AgentEpisodePlanUpdateRequest,
) -> Dict[str, Any]:
    context = await _get_context(db, production_id, user.id, lock=True)
    _assert_editable(context)
    _check_version(context.step, payload.expected_version)
    items = _plan_items(context)
    index = _find_plan_index(items, plan_id)
    update_data = payload.model_dump(
        exclude={"expected_version", "position"},
        exclude_none=True,
    )
    items[index] = {**items[index], **update_data}
    if payload.position is not None:
        if payload.position > len(items):
            raise AppException(
                f"position 不能超过当前剧集数量 {len(items)}",
                code=40054,
                status_code=400,
            )
        item = items.pop(index)
        items.insert(payload.position - 1, item)
    await _save_mutation(
        db,
        context,
        items,
        user.id,
        "episode_plan.updated",
        {"plan_id": str(plan_id)},
    )
    return _document(context)


async def merge_episode_plans(
    db: AsyncSession,
    production_id: UUID,
    user: User,
    payload: AgentEpisodePlanMergeRequest,
) -> Dict[str, Any]:
    context = await _get_context(db, production_id, user.id, lock=True)
    _assert_editable(context)
    _check_version(context.step, payload.expected_version)
    items = _plan_items(context)
    indexes = sorted(_find_plan_index(items, plan_id) for plan_id in payload.plan_ids)
    if indexes[1] != indexes[0] + 1:
        raise AppException("只能合并相邻的剧集规划", code=40055, status_code=400)
    first, second = items[indexes[0]], items[indexes[1]]
    first_range = _source_range(context, first)
    second_range = _source_range(context, second)
    merged = _merge_items(
        first,
        second,
        payload.title,
        payload.content,
        min(first_range[0], second_range[0]),
        max(first_range[1], second_range[1]),
    )
    items[indexes[0] : indexes[1] + 1] = [merged]
    await _save_mutation(
        db,
        context,
        items,
        user.id,
        "episode_plan.merged",
        {
            "plan_ids": [str(value) for value in payload.plan_ids],
            "result_plan_id": merged["plan_id"],
        },
    )
    return _document(context)


async def split_episode_plan(
    db: AsyncSession,
    production_id: UUID,
    plan_id: UUID,
    user: User,
    payload: AgentEpisodePlanSplitRequest,
) -> Dict[str, Any]:
    context = await _get_context(db, production_id, user.id, lock=True)
    _assert_editable(context)
    _check_version(context.step, payload.expected_version)
    items = _plan_items(context)
    index = _find_plan_index(items, plan_id)
    item = items[index]
    source_start, source_end = _source_range(context, item)
    if not source_start < payload.split_at < source_end:
        raise AppException(
            f"split_at 必须位于原文区间 ({source_start}, {source_end}) 内",
            code=40056,
            status_code=400,
        )
    first, second = _split_item(context, item, payload, source_start, source_end)
    items[index : index + 1] = [first, second]
    await _save_mutation(
        db,
        context,
        items,
        user.id,
        "episode_plan.split",
        {
            "plan_id": str(plan_id),
            "result_plan_ids": [first["plan_id"], second["plan_id"]],
            "split_at": payload.split_at,
        },
    )
    return _document(context)


async def preview_episode_plan_impact(
    db: AsyncSession,
    production_id: UUID,
    user_id: UUID,
) -> Dict[str, Any]:
    context = await _get_context(db, production_id, user_id, lock=False)
    items = _plan_items(context)
    active_count_result = await db.execute(
        select(func.count())
        .select_from(ProjectChapter)
        .where(
            ProjectChapter.project_id == context.production.project_id,
            ProjectChapter.user_id == user_id,
            ProjectChapter.is_enabled.is_(True),
        )
    )
    existing_active_count = int(active_count_result.scalar_one() or 0)
    ranges = [_source_range(context, item) for item in items]
    warnings = _impact_warnings(context, items, ranges, existing_active_count)
    source_length = len(context.source.content)
    return {
        "production_id": context.production.id,
        "version": _plan_version(context.step),
        "chapter_count": len(items),
        "existing_active_chapter_count": existing_active_count,
        "will_create_count": 0 if context.checkpoint.status == "approved" else len(items),
        "source_covered_characters": _covered_characters(ranges, source_length),
        "source_character_count": context.source.character_count,
        "warnings": warnings,
    }


async def confirm_episode_plan(
    db: AsyncSession,
    production_id: UUID,
    user: User,
    payload: AgentEpisodePlanConfirmRequest,
) -> Dict[str, Any]:
    context = await _get_context(db, production_id, user.id, lock=True)
    if (
        uses_agent_workflow_v2(context.production)
        and context.checkpoint.checkpoint_type == "script_review"
    ):
        raise AppException(
            "新流程请使用剧本处理包确认接口",
            code=40947,
            status_code=409,
        )
    _check_version(context.step, payload.expected_version)
    version = _plan_version(context.step)
    if context.checkpoint.status == "approved":
        return {
            "production_id": context.production.id,
            "version": version,
            "chapter_ids": _stored_chapter_ids(context.checkpoint),
            "created_count": 0,
            "already_confirmed": True,
        }
    _assert_editable(context)
    items = _validate_confirmable_items(context)
    chapter_ids, created_count, _changed_chapter_ids = await materialize_episode_chapters(
        db,
        context,
        user.id,
        items,
        version,
    )

    now = beijing_datetime()
    context.checkpoint.status = transition_checkpoint_status(
        context.checkpoint.status,
        "approved",
    )
    context.checkpoint.approved_by = user.id
    context.checkpoint.approved_at = now
    context.checkpoint.extra = {
        **(context.checkpoint.extra or {}),
        "confirmed_version": version,
        "confirmation_idempotency_key": payload.idempotency_key,
        "materialized_chapter_ids": [str(value) for value in chapter_ids],
    }
    context.step.status = transition_step_status(context.step.status, "completed")
    context.step.finished_at = now
    context.production.status = transition_production_status(
        context.production.status,
        "planning",
    )
    context.production.current_stage = "story_bible"
    context.production.lock_version += 1
    db.add(
        AgentEvent(
            production_id=context.production.id,
            step_id=context.step.id,
            actor_user_id=user.id,
            event_type="episode_plan.confirmed",
            source="user",
            payload={
                "version": version,
                "chapter_ids": [str(value) for value in chapter_ids],
                "idempotency_key": payload.idempotency_key,
            },
        )
    )
    await db.commit()
    return {
        "production_id": context.production.id,
        "version": version,
        "chapter_ids": chapter_ids,
        "created_count": created_count,
        "already_confirmed": False,
    }


async def materialize_episode_chapters(
    db: AsyncSession,
    context: EpisodePlanContext,
    user_id: UUID,
    items: List[Dict[str, Any]],
    version: int,
) -> Tuple[List[UUID], int, List[UUID]]:
    existing_result = await db.execute(
        select(ProjectChapter).where(
            ProjectChapter.project_id == context.production.project_id,
            ProjectChapter.user_id == user_id,
            ProjectChapter.extra["agent_production_id"].as_string() == str(context.production.id),
        )
    )
    existing_chapters = list(existing_result.scalars().all())
    existing_by_plan_id = {
        str((chapter.extra or {}).get("episode_plan_id")): chapter
        for chapter in existing_chapters
        if (chapter.extra or {}).get("episode_plan_id")
    }
    existing_by_source_range = {
        (
            _optional_int((chapter.extra or {}).get("source_start")),
            _optional_int((chapter.extra or {}).get("source_end")),
        ): chapter
        for chapter in existing_chapters
        if _optional_int((chapter.extra or {}).get("source_start")) is not None
        and _optional_int((chapter.extra or {}).get("source_end")) is not None
    }
    chapter_ids: List[UUID] = []
    changed_chapter_ids: List[UUID] = []
    created_count = 0
    for index, item in enumerate(items):
        plan_id = str(item["plan_id"])
        chapter = existing_by_plan_id.get(plan_id)
        source_start, source_end = _source_range(context, item)
        if chapter is None:
            chapter = existing_by_source_range.get((source_start, source_end))
        if chapter is None:
            processed_content = str(item.get("content") or "").strip()
            episode_number = int(item.get("episode_number") or index + 1)
            chapter = ProjectChapter(
                project_id=context.production.project_id,
                user_id=user_id,
                ai_model_id=_optional_uuid(
                    (context.production.production_spec or {}).get("text_model_id")
                ),
                title=str(item["title"])[:128],
                content=context.source.content[source_start:source_end],
                processed_content=processed_content,
                process_status="success",
                sort_order=index,
                extra={
                    "agent_production_id": str(context.production.id),
                    "agent_step_id": str(context.step.id),
                    "episode_plan_id": plan_id,
                    "episode_plan_version": version,
                    "episode_number": episode_number,
                    "source_document_id": str(context.source.id),
                    "source_start": source_start,
                    "source_end": source_end,
                    "opening_hook": item.get("opening_hook"),
                    "goal": item.get("goal"),
                    "conflict": item.get("conflict"),
                    "climax": item.get("climax"),
                    "ending_hook": item.get("ending_hook"),
                    "estimated_duration_seconds": item.get("estimated_duration_seconds"),
                    "estimated_shot_count": item.get("estimated_shot_count"),
                    "characters": item.get("characters") or [],
                    "character_variants": item.get("character_variants") or [],
                    "scenes": item.get("scenes") or [],
                    "scene_variants": item.get("scene_variants") or [],
                    "props": item.get("props") or [],
                    "prop_variants": item.get("prop_variants") or [],
                    "continuity_notes": item.get("continuity_notes") or [],
                },
                is_enabled=True,
            )
            db.add(chapter)
            await db.flush()
            initialize_agent_episode_workflow_states(
                db,
                context.production,
                chapter,
                episode_number,
            )
            created_count += 1
            changed_chapter_ids.append(chapter.id)
        else:
            processed_content = str(item.get("content") or "").strip()
            episode_number = int(item.get("episode_number") or index + 1)
            source_content = context.source.content[source_start:source_end]
            previous_extra = dict(chapter.extra or {})
            planning_extra = {
                "opening_hook": item.get("opening_hook"),
                "goal": item.get("goal"),
                "conflict": item.get("conflict"),
                "climax": item.get("climax"),
                "ending_hook": item.get("ending_hook"),
                "estimated_duration_seconds": item.get("estimated_duration_seconds"),
                "estimated_shot_count": item.get("estimated_shot_count"),
                "characters": item.get("characters") or [],
                "character_variants": item.get("character_variants") or [],
                "scenes": item.get("scenes") or [],
                "scene_variants": item.get("scene_variants") or [],
                "props": item.get("props") or [],
                "prop_variants": item.get("prop_variants") or [],
                "continuity_notes": item.get("continuity_notes") or [],
            }
            content_changed = any(
                (
                    chapter.title != str(item["title"])[:128],
                    chapter.content != source_content,
                    (chapter.processed_content or "") != processed_content,
                    _optional_int(previous_extra.get("source_start")) != source_start,
                    _optional_int(previous_extra.get("source_end")) != source_end,
                    any(
                        key in previous_extra
                        and (
                            (previous_extra.get(key) or []) != value
                            if isinstance(value, list)
                            else previous_extra.get(key) != value
                        )
                        for key, value in planning_extra.items()
                    ),
                )
            )
            chapter.title = str(item["title"])[:128]
            chapter.content = source_content
            chapter.processed_content = processed_content
            chapter.process_status = "success"
            chapter.sort_order = index
            chapter.extra = {
                **previous_extra,
                "episode_plan_id": plan_id,
                "episode_plan_version": version,
                "episode_number": episode_number,
                "source_document_id": str(context.source.id),
                "source_start": source_start,
                "source_end": source_end,
                **planning_extra,
                **({"storyboard_analysis_status": "invalidated"} if content_changed else {}),
            }
            if content_changed:
                changed_chapter_ids.append(chapter.id)
        chapter_ids.append(chapter.id)
    active_ids = set(chapter_ids)
    for chapter in existing_chapters:
        if chapter.id not in active_ids and chapter.is_enabled:
            chapter.is_enabled = False
    return chapter_ids, created_count, changed_chapter_ids


async def _get_context(
    db: AsyncSession,
    production_id: UUID,
    user_id: UUID,
    *,
    lock: bool,
) -> EpisodePlanContext:
    production_query = (
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
        production_query = production_query.with_for_update(of=AgentProduction)
    production_result = await db.execute(production_query)
    production = production_result.scalar_one_or_none()
    if production is None:
        raise AppException("整剧任务不存在", code=40430, status_code=404)
    source = await db.get(ProjectSourceDocument, production.source_document_id)
    if (
        source is None
        or source.project_id != production.project_id
        or source.user_id != user_id
    ):
        raise AppException("整剧原文不存在", code=40431, status_code=404)

    step_query = select(AgentStep).where(
        AgentStep.production_id == production.id,
        AgentStep.stage == "source_analysis",
        AgentStep.scope_type == "production",
        AgentStep.scope_id == production.id,
        AgentStep.input_version == source.version,
    )
    if lock:
        step_query = step_query.with_for_update(of=AgentStep)
    step_result = await db.execute(step_query)
    step = step_result.scalar_one_or_none()
    if step is None or not isinstance((step.extra or {}).get("episode_plan"), dict):
        raise AppException("分集规划尚未生成", code=40915, status_code=409)

    checkpoint_query = select(AgentCheckpoint).where(
        AgentCheckpoint.step_id == step.id,
        AgentCheckpoint.checkpoint_type
        == ("script_review" if uses_agent_workflow_v2(production) else "episode_plan_review"),
    )
    if lock:
        checkpoint_query = checkpoint_query.with_for_update(of=AgentCheckpoint)
    checkpoint_result = await db.execute(checkpoint_query)
    checkpoint = checkpoint_result.scalar_one_or_none()
    if checkpoint is None:
        raise AppException("分集规划确认点不存在", code=40916, status_code=409)
    return EpisodePlanContext(production, step, checkpoint, source)


def _document(context: EpisodePlanContext) -> Dict[str, Any]:
    plan = (context.step.extra or {}).get("episode_plan") or {}
    return {
        "production_id": context.production.id,
        "step_id": context.step.id,
        "checkpoint_id": context.checkpoint.id,
        "version": _plan_version(context.step),
        "status": context.checkpoint.status,
        "planning_summary": str(plan.get("planning_summary") or ""),
        "items": _plan_items(context),
    }


def _plan_items(context: EpisodePlanContext) -> List[Dict[str, Any]]:
    plan = (context.step.extra or {}).get("episode_plan") or {}
    raw_items = plan.get("episodes") or []
    if not isinstance(raw_items, list) or not raw_items:
        raise AppException("分集规划内容为空", code=40917, status_code=409)
    items = []
    for index, raw_item in enumerate(raw_items):
        if not isinstance(raw_item, dict):
            raise AppException("分集规划数据损坏", code=50045, status_code=500)
        item = dict(raw_item)
        item["plan_id"] = str(_plan_id(context, item, index))
        item["episode_number"] = index + 1
        item["title"] = str(item.get("title") or f"第 {index + 1} 集").strip()
        item["content"] = str(
            item.get("content") or item.get("logline") or item.get("goal") or item["title"]
        ).strip()
        source_start, source_end = _source_range(context, item)
        item["source_content"] = context.source.content[source_start:source_end]
        item["logline"] = str(item.get("logline") or "").strip()
        item["opening_hook"] = str(item.get("opening_hook") or "").strip()
        item["goal"] = str(item.get("goal") or "").strip()
        item["conflict"] = str(item.get("conflict") or "").strip()
        item["climax"] = str(item.get("climax") or "").strip()
        item["ending_hook"] = str(item.get("ending_hook") or item.get("hook") or "").strip()
        item.pop("estimated_duration_seconds", None)
        item.pop("estimated_shot_count", None)
        for key in (
            "source_block_ids",
            "characters",
            "character_variants",
            "scenes",
            "scene_variants",
            "props",
            "prop_variants",
            "continuity_notes",
        ):
            item[key] = item.get(key) if isinstance(item.get(key), list) else []
        items.append(item)
    return items


async def _save_mutation(
    db: AsyncSession,
    context: EpisodePlanContext,
    items: List[Dict[str, Any]],
    actor_user_id: UUID,
    event_type: str,
    event_payload: Dict[str, Any],
) -> None:
    if not items or len(items) > 200:
        raise AppException("剧集规划数量必须在 1 到 200 之间", code=40057, status_code=400)
    if len({str(item.get("plan_id")) for item in items}) != len(items):
        raise AppException("剧集规划标识重复", code=50046, status_code=500)
    stored_items = []
    for index, item in enumerate(items, start=1):
        stored_item = {**item, "episode_number": index}
        stored_item.pop("source_content", None)
        stored_items.append(stored_item)
    current_plan = (context.step.extra or {}).get("episode_plan") or {}
    new_version = _plan_version(context.step) + 1
    context.step.output_version = new_version
    context.step.extra = {
        **(context.step.extra or {}),
        "episode_plan": {**current_plan, "episodes": stored_items},
    }
    if uses_agent_workflow_v2(context.production):
        await sync_production_candidate_episode_numbers(
            db,
            context.production.id,
            (context.step.extra or {}).get("episode_plan") or {},
        )
        await sync_production_variant_episode_numbers(
            db,
            context.production.id,
            (context.step.extra or {}).get("episode_plan") or {},
        )
    context.checkpoint.summary = (
        f"分集规划已更新为第 {new_version} 版，共 {len(items)} 集，请审核。"
    )
    context.checkpoint.impact = {
        **(context.checkpoint.impact or {}),
        "episode_count": len(items),
        "plan_version": new_version,
    }
    context.production.lock_version += 1
    db.add(
        AgentEvent(
            production_id=context.production.id,
            step_id=context.step.id,
            actor_user_id=actor_user_id,
            event_type=event_type,
            source="user",
            payload={"from_version": new_version - 1, "to_version": new_version, **event_payload},
        )
    )
    await db.commit()


def _assert_editable(context: EpisodePlanContext) -> None:
    if context.production.status != "waiting_approval" or context.step.status != "waiting_approval":
        raise AppException("当前整剧状态不允许修改分集规划", code=40918, status_code=409)
    if context.checkpoint.status != "pending":
        raise AppException("分集规划已经确认，不能继续修改", code=40919, status_code=409)


def _check_version(step: AgentStep, expected_version: int) -> None:
    current_version = _plan_version(step)
    if expected_version != current_version:
        raise AppException(
            f"分集规划版本冲突，当前版本为 {current_version}",
            code=40920,
            status_code=409,
            data={
                "expected_version": expected_version,
                "current_version": current_version,
            },
        )


def _plan_version(step: AgentStep) -> int:
    return max(1, int(step.output_version or 1))


def _plan_id(context: EpisodePlanContext, item: Dict[str, Any], index: int) -> UUID:
    existing = _optional_uuid(item.get("plan_id") or item.get("id"))
    if existing is not None:
        return existing
    fingerprint = (
        f"{context.production.id}:{context.step.id}:{index}:"
        f"{item.get('source_start')}:{item.get('source_end')}:{item.get('title')}"
    )
    return uuid5(NAMESPACE_URL, fingerprint)


def _find_plan_index(items: List[Dict[str, Any]], plan_id: UUID) -> int:
    for index, item in enumerate(items):
        if str(item.get("plan_id")) == str(plan_id):
            return index
    raise AppException("剧集规划不存在", code=40432, status_code=404)


def _merge_items(
    first: Dict[str, Any],
    second: Dict[str, Any],
    title: Optional[str],
    content: Optional[str],
    source_start: int,
    source_end: int,
) -> Dict[str, Any]:
    source_plan_ids = _unique_values(
        list(first.get("source_plan_ids") or [])
        + [first["plan_id"]]
        + list(second.get("source_plan_ids") or [])
        + [second["plan_id"]]
    )
    merged = {
        **first,
        "plan_id": str(uuid4()),
        "title": (title or f"{first['title']} / {second['title']}").strip(),
        "content": (content or _join_text(first.get("content"), second.get("content"))).strip(),
        "logline": _join_text(first.get("logline"), second.get("logline")),
        "goal": _join_text(first.get("goal"), second.get("goal")),
        "conflict": _join_text(first.get("conflict"), second.get("conflict")),
        "climax": str(second.get("climax") or first.get("climax") or ""),
        "ending_hook": str(second.get("ending_hook") or ""),
        "source_block_ids": _unique_values(
            list(first.get("source_block_ids") or []) + list(second.get("source_block_ids") or [])
        ),
        "characters": _unique_values(
            list(first.get("characters") or []) + list(second.get("characters") or [])
        ),
        "character_variants": _unique_values(
            list(first.get("character_variants") or [])
            + list(second.get("character_variants") or [])
        ),
        "scenes": _unique_values(
            list(first.get("scenes") or []) + list(second.get("scenes") or [])
        ),
        "scene_variants": _unique_values(
            list(first.get("scene_variants") or []) + list(second.get("scene_variants") or [])
        ),
        "props": _unique_values(list(first.get("props") or []) + list(second.get("props") or [])),
        "prop_variants": _unique_values(
            list(first.get("prop_variants") or []) + list(second.get("prop_variants") or [])
        ),
        "continuity_notes": _unique_values(
            list(first.get("continuity_notes") or []) + list(second.get("continuity_notes") or [])
        ),
        "source_plan_ids": source_plan_ids,
        "source_start": source_start,
        "source_end": source_end,
    }
    return merged


def _split_item(
    context: EpisodePlanContext,
    item: Dict[str, Any],
    payload: AgentEpisodePlanSplitRequest,
    source_start: int,
    source_end: int,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    ratio = (payload.split_at - source_start) / (source_end - source_start)
    original_content = str(item.get("content") or "")
    first_content, second_content = _split_text(original_content, ratio)
    if payload.first_content:
        first_content = payload.first_content.strip()
    if payload.second_content:
        second_content = payload.second_content.strip()
    if not first_content:
        first_content = context.source.content[source_start : payload.split_at].strip()
    if not second_content:
        second_content = context.source.content[payload.split_at : source_end].strip()

    source_plan_ids = _unique_values(list(item.get("source_plan_ids") or []) + [item["plan_id"]])
    first = {
        **item,
        "plan_id": str(uuid4()),
        "title": (payload.first_title or f"{item['title']}（上）").strip(),
        "content": first_content,
        "ending_hook": str(item.get("climax") or item.get("ending_hook") or "待续"),
        "source_start": source_start,
        "source_end": payload.split_at,
        "source_block_ids": [],
        "source_plan_ids": source_plan_ids,
    }
    second = {
        **item,
        "plan_id": str(uuid4()),
        "title": (payload.second_title or f"{item['title']}（下）").strip(),
        "content": second_content,
        "opening_hook": str(item.get("climax") or item.get("opening_hook") or "继续剧情"),
        "source_start": payload.split_at,
        "source_end": source_end,
        "source_block_ids": [],
        "source_plan_ids": source_plan_ids,
    }
    return first, second


def _validate_confirmable_items(context: EpisodePlanContext) -> List[Dict[str, Any]]:
    items = _plan_items(context)
    validated = validate_agent_stage_output(
        "episode_planning",
        {"episodes": items},
    )["episodes"]
    for item in validated:
        source_start, source_end = _source_range(context, item)
        if not 0 <= source_start < source_end <= len(context.source.content):
            raise AppException("剧集规划原文范围越界", code=40058, status_code=400)
        if not str(item.get("content") or "").strip():
            raise AppException("剧集规划内容不能为空", code=40059, status_code=400)
    return validated


def _source_range(context: EpisodePlanContext, item: Dict[str, Any]) -> Tuple[int, int]:
    source_start = _optional_int(item.get("source_start"))
    source_end = _optional_int(item.get("source_end"))
    if source_start is not None and source_end is not None and source_end > source_start:
        return source_start, source_end
    block_ids = item.get("source_block_ids") or []
    chunk_by_index = {
        int(chunk["index"]): chunk
        for chunk in ((context.step.extra or {}).get("chunks") or [])
        if isinstance(chunk, dict) and str(chunk.get("index", "")).isdigit()
    }
    chunks = [chunk_by_index.get(_int(block_id, -1)) for block_id in block_ids]
    chunks = [chunk for chunk in chunks if chunk is not None]
    if chunks:
        return min(int(chunk["start_offset"]) for chunk in chunks), max(
            int(chunk["end_offset"]) for chunk in chunks
        )
    raise AppException(
        f"剧集规划 {item.get('episode_number')} 缺少有效原文范围",
        code=40060,
        status_code=400,
    )


def _impact_warnings(
    context: EpisodePlanContext,
    items: List[Dict[str, Any]],
    ranges: List[Tuple[int, int]],
    existing_active_count: int,
) -> List[str]:
    warnings = []
    if existing_active_count:
        warnings.append(f"项目已有 {existing_active_count} 个有效章节，新章节将追加到现有章节之后")
    source_length = len(context.source.content)
    if any(start < 0 or end > source_length for start, end in ranges):
        warnings.append("存在超出原文范围的剧集，确认前必须修正")
    if _overlap_characters(ranges) > 0:
        warnings.append("相邻剧集的原文范围存在重叠，请确认是否会造成重复剧情")
    if _covered_characters(ranges, source_length) < source_length:
        warnings.append("分集规划未覆盖全部原文字符，请确认遗漏内容是否需要保留")
    if any(not str(item.get("content") or "").strip() for item in items):
        warnings.append("存在内容为空的剧集，确认前必须补齐")
    if context.checkpoint.status == "approved":
        warnings.append("该版本已经确认，不会重复创建章节")
    return warnings


def _covered_characters(
    ranges: List[Tuple[int, int]],
    source_length: Optional[int] = None,
) -> int:
    if not ranges:
        return 0
    merged = []
    for start, end in sorted(ranges):
        if source_length is not None:
            start = max(0, min(start, source_length))
            end = max(0, min(end, source_length))
        if end <= start:
            continue
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return sum(end - start for start, end in merged)


def _overlap_characters(ranges: List[Tuple[int, int]]) -> int:
    overlap = 0
    covered_until: Optional[int] = None
    for start, end in sorted(ranges):
        if covered_until is not None and start < covered_until:
            overlap += max(0, min(end, covered_until) - start)
        covered_until = end if covered_until is None else max(covered_until, end)
    return overlap


def _stored_chapter_ids(checkpoint: AgentCheckpoint) -> List[UUID]:
    result = []
    for value in (checkpoint.extra or {}).get("materialized_chapter_ids") or []:
        parsed = _optional_uuid(value)
        if parsed is not None:
            result.append(parsed)
    return result


def _unique_values(values: List[Any]) -> List[Any]:
    result = []
    seen = set()
    for value in values:
        key = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _split_text(content: str, ratio: float) -> Tuple[str, str]:
    if not content:
        return "", ""
    split_index = min(len(content) - 1, max(1, round(len(content) * ratio)))
    return content[:split_index].strip(), content[split_index:].strip()


def _join_text(first: Any, second: Any) -> str:
    return "\n".join(
        value for value in (str(first or "").strip(), str(second or "").strip()) if value
    )


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _optional_int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _optional_uuid(value: Any) -> Optional[UUID]:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value)) if value else None
    except (TypeError, ValueError):
        return None
