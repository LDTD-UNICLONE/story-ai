import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import AppException
from app.core.public_messages import sanitize_public_message
from app.core.timezone import beijing_datetime
from app.models.agent_production import (
    AgentCheckpoint,
    AgentEvent,
    AgentProduction,
    AgentStep,
    ProjectSourceDocument,
)
from app.models.ai_model import AiModel
from app.models.agent_story_bible import SeriesBibleVersion
from app.models.project_chapter import ProjectChapter
from app.models.task_record import UserTaskRecord
from app.services.agent_production_state import (
    transition_production_status,
    transition_step_status,
)
from app.services.agent_source_text import (
    SourceTextChunk,
    split_source_text,
    validate_agent_stage_output,
)
from app.services.agent_story_bibles import initialize_script_assets
from app.services.agent_workflow import uses_agent_workflow_v2
from app.services.model_points import (
    calculate_text_submission_points_cost,
    ensure_model_minimum_balance,
    settle_text_task_points,
)
from app.services.points import change_user_points, consume_user_points
from app.services.prompts import render_system_prompt
from app.services.task_records import create_user_task_record


CHUNK_ANALYSIS = "chunk_analysis"
GLOBAL_MERGE = "global_merge"
ASSET_ANALYSIS = "asset_analysis"
EPISODE_PLANNING = "episode_planning"

_GENERATION_TYPES = {
    CHUNK_ANALYSIS: "agent_source_chunk_analysis",
    GLOBAL_MERGE: "agent_source_global_merge",
    ASSET_ANALYSIS: "agent_asset_analysis",
    EPISODE_PLANNING: "agent_episode_planning",
}


@dataclass(frozen=True)
class SourceAnalysisAdvanceResult:
    task_record_ids: List[UUID]
    completed: bool = False


async def advance_source_analysis(
    db: AsyncSession,
    production_id: UUID,
    step_id: UUID,
) -> SourceAnalysisAdvanceResult:
    production = await _lock_production(db, production_id)
    step = await _lock_step(db, production_id, step_id)
    if production is None or step is None or step.stage != "source_analysis":
        return SourceAnalysisAdvanceResult([])
    if production.status not in {"planning", "running"}:
        return SourceAnalysisAdvanceResult([])
    if step.status in {"waiting_approval", "completed", "skipped", "invalidated"}:
        return SourceAnalysisAdvanceResult([], completed=step.status in {"waiting_approval", "completed"})

    source = await db.get(ProjectSourceDocument, production.source_document_id)
    if (
        source is None
        or source.project_id != production.project_id
        or source.user_id != production.user_id
        or not source.content
    ):
        raise AppException("整剧原文不存在或为空", code=40431, status_code=404)
    ai_model = await _get_text_model(db, production)
    chunks = split_source_text(source.content, max(1, settings.agent_source_chunk_characters))
    if not chunks:
        raise AppException("整剧原文不能为空", code=40049, status_code=400)

    recovering = bool((step.extra or {}).get("initialized")) and (
        step.status == "queued" or bool((step.extra or {}).get("redispatch_requested"))
    )
    if recovering:
        step.error_summary = None
        step.finished_at = None
    if step.status == "failed":
        step.status = transition_step_status(step.status, "queued")
    if step.status == "queued":
        step.status = transition_step_status(step.status, "running")
        step.attempt_count += 1
        step.started_at = step.started_at or beijing_datetime()
        step.error_summary = None
    entries = _chunk_entries(step, chunks)
    if uses_agent_workflow_v2(production):
        entries = [
            {key: value for key, value in entry.items() if key != "task_record_id"}
            for entry in entries
        ]
    source.parse_status = "running"
    step.progress_total = 2 if uses_agent_workflow_v2(production) else len(chunks) + 2
    step.extra = {
        **(step.extra or {}),
        "initialized": True,
        "source_content_hash": source.content_hash,
        "chunk_characters": settings.agent_source_chunk_characters,
        "chunks": entries,
        "redispatch_requested": False,
    }
    if uses_agent_workflow_v2(production):
        return await _advance_two_task_source_analysis(
            db,
            production,
            step,
            source,
            ai_model,
            recovering=recovering,
        )

    records = await _load_task_records(db, [entry.get("task_record_id") for entry in entries])
    created: List[UserTaskRecord] = []
    for entry, chunk in zip(entries, chunks):
        record = records.get(str(entry.get("task_record_id")))
        if record is not None and record.status == "pending" and recovering:
            record.extra = {**(record.extra or {}), "dispatch_state": "pending"}
        if record is not None and record.status == "failed" and recovering:
            record = None
            entry.pop("task_record_id", None)
        if record is None:
            record = await _create_agent_text_task(
                db,
                production,
                step,
                ai_model,
                CHUNK_ANALYSIS,
                title=f"整剧分块解析 {chunk.index + 1}/{len(chunks)}",
                role_extra={
                    "chunk_index": chunk.index,
                    "chunk_count": len(chunks),
                    "start_offset": chunk.start_offset,
                    "end_offset": chunk.end_offset,
                    "content_hash": chunk.content_hash,
                },
            )
            entry["task_record_id"] = str(record.id)
            created.append(record)

    await db.flush()
    if created:
        await db.commit()
        return SourceAnalysisAdvanceResult([record.id for record in created])

    chunk_records = await _load_task_records(db, [entry.get("task_record_id") for entry in entries])
    ordered_chunk_records = [chunk_records.get(str(entry.get("task_record_id"))) for entry in entries]
    step.progress_current = sum(record is not None and record.status == "success" for record in ordered_chunk_records)
    dispatchable = _undispatched_records(ordered_chunk_records)
    if dispatchable:
        await db.commit()
        return SourceAnalysisAdvanceResult([record.id for record in dispatchable])
    if not all(record is not None and record.status == "success" for record in ordered_chunk_records):
        await db.commit()
        return SourceAnalysisAdvanceResult([])

    analysis_record = await _stage_task_record(db, step, "merge_task_record_id")
    if analysis_record is not None and analysis_record.status == "pending" and recovering:
        analysis_record.extra = {
            **(analysis_record.extra or {}),
            "dispatch_state": "pending",
        }
    if analysis_record is not None and analysis_record.status == "failed" and recovering:
        analysis_record = None
    if analysis_record is None:
        analysis_record = await _create_agent_text_task(
            db,
            production,
            step,
            ai_model,
            GLOBAL_MERGE,
            title="整剧全局分析合并",
            role_extra={},
        )
        step.extra = {
            **(step.extra or {}),
            "merge_task_record_id": str(analysis_record.id),
        }
        await db.commit()
        return SourceAnalysisAdvanceResult([analysis_record.id])
    if analysis_record.status != "success":
        await db.commit()
        return SourceAnalysisAdvanceResult(
            [analysis_record.id] if _is_undispatched(analysis_record) else []
        )
    step.progress_current = len(chunks) + 1

    plan_record = await _stage_task_record(db, step, "episode_plan_task_record_id")
    if plan_record is not None and plan_record.status == "pending" and recovering:
        plan_record.extra = {
            **(plan_record.extra or {}),
            "dispatch_state": "pending",
        }
    if plan_record is not None and plan_record.status == "failed" and recovering:
        plan_record = None
    if plan_record is None:
        plan_record = await _create_agent_text_task(
            db,
            production,
            step,
            ai_model,
            EPISODE_PLANNING,
            title="整剧分集规划",
            role_extra={},
        )
        step.extra = {
            **(step.extra or {}),
            "episode_plan_task_record_id": str(plan_record.id),
        }
        await db.commit()
        return SourceAnalysisAdvanceResult([plan_record.id])
    if plan_record.status != "success":
        await db.commit()
        return SourceAnalysisAdvanceResult(
            [plan_record.id] if _is_undispatched(plan_record) else []
        )

    await _complete_source_analysis(
        db,
        production,
        step,
        source,
        analysis_record,
        plan_record,
    )
    await db.commit()
    return SourceAnalysisAdvanceResult([], completed=True)


async def _advance_two_task_source_analysis(
    db: AsyncSession,
    production: AgentProduction,
    step: AgentStep,
    source: ProjectSourceDocument,
    ai_model: AiModel,
    *,
    recovering: bool,
) -> SourceAnalysisAdvanceResult:
    plan_record = await _stage_task_record(db, step, "episode_plan_task_record_id")
    if plan_record is not None and plan_record.status == "pending" and recovering:
        plan_record.extra = {**(plan_record.extra or {}), "dispatch_state": "pending"}
    if plan_record is not None and plan_record.status == "failed" and recovering:
        plan_record = None
    if plan_record is None:
        plan_record = await _create_agent_text_task(
            db,
            production,
            step,
            ai_model,
            EPISODE_PLANNING,
            title="整剧分集规划",
            role_extra={},
        )
        step.extra = {
            **(step.extra or {}),
            "episode_plan_task_record_id": str(plan_record.id),
        }
        await db.commit()
        return SourceAnalysisAdvanceResult([plan_record.id])
    if plan_record.status != "success":
        await db.commit()
        return SourceAnalysisAdvanceResult(
            [plan_record.id] if _is_undispatched(plan_record) else []
        )

    episode_plan = _validated_episode_plan_result(plan_record, step, source.content)
    step.progress_current = 1
    analysis_record = await _stage_task_record(db, step, "asset_analysis_task_record_id")
    if analysis_record is not None and analysis_record.status == "pending" and recovering:
        analysis_record.extra = {**(analysis_record.extra or {}), "dispatch_state": "pending"}
    if analysis_record is not None and analysis_record.status == "failed" and recovering:
        analysis_record = None
    if analysis_record is None:
        analysis_record = await _create_agent_text_task(
            db,
            production,
            step,
            ai_model,
            ASSET_ANALYSIS,
            title="整剧资产分析与剧情化设计",
            role_extra={},
        )
        step.extra = {
            **(step.extra or {}),
            "asset_analysis_task_record_id": str(analysis_record.id),
        }
        await db.commit()
        return SourceAnalysisAdvanceResult([analysis_record.id])
    if analysis_record.status != "success":
        await db.commit()
        return SourceAnalysisAdvanceResult(
            [analysis_record.id] if _is_undispatched(analysis_record) else []
        )

    scope_text, scope_start, _scope_end = _analysis_source_scope(step, source.content)
    analysis_result = validate_agent_stage_output(
        ASSET_ANALYSIS,
        (analysis_record.extra or {}).get("parsed_result") or {},
        source_text=scope_text,
        source_start=scope_start,
    )
    analysis_record.extra = {
        **(analysis_record.extra or {}),
        "parsed_result": analysis_result,
    }
    episode_plan = await _merge_incremental_episode_plan(
        db,
        production,
        step,
        episode_plan,
    )
    plan_record.extra = {
        **(plan_record.extra or {}),
        "parsed_result": episode_plan,
    }
    await _complete_source_analysis(
        db,
        production,
        step,
        source,
        analysis_record,
        plan_record,
    )
    await db.commit()
    return SourceAnalysisAdvanceResult([], completed=True)


def _validated_episode_plan_result(
    plan_record: UserTaskRecord,
    step: AgentStep,
    source_content: str,
) -> Dict[str, Any]:
    scope_text, scope_start, scope_end = _analysis_source_scope(step, source_content)
    episode_plan = validate_agent_stage_output(
        EPISODE_PLANNING,
        (plan_record.extra or {}).get("parsed_result") or {},
        source_text=scope_text,
        source_start=scope_start,
    )
    episodes = episode_plan.get("episodes") or []
    if (
        not episodes
        or int(episodes[0].get("source_start", -1)) != scope_start
        or int(episodes[-1].get("source_end", -1)) != scope_end
    ):
        raise AppException("分集规划未连续覆盖本次分析原文", code=50244, status_code=502)
    return episode_plan


async def mark_agent_tasks_dispatched(db: AsyncSession, task_record_ids: List[UUID]) -> None:
    if not task_record_ids:
        return
    result = await db.execute(select(UserTaskRecord).where(UserTaskRecord.id.in_(task_record_ids)).with_for_update())
    now = beijing_datetime().isoformat()
    for record in result.scalars().all():
        record.extra = {
            **(record.extra or {}),
            "dispatch_state": "dispatched",
            "dispatched_at": now,
        }
    await db.commit()


async def build_agent_text_prompt(
    db: AsyncSession,
    task_record: UserTaskRecord,
) -> str:
    extra = task_record.extra or {}
    production_id = _uuid(extra.get("agent_production_id"))
    step_id = _uuid(extra.get("agent_step_id"))
    production = await db.get(AgentProduction, production_id)
    step = await db.get(AgentStep, step_id)
    if production is None or step is None:
        raise AppException("整剧任务或分析步骤不存在", code=40430, status_code=404)
    source = await db.get(ProjectSourceDocument, production.source_document_id)
    if (
        source is None
        or source.project_id != production.project_id
        or source.user_id != production.user_id
    ):
        raise AppException("整剧原文不存在", code=40431, status_code=404)

    stage = str(extra.get("agent_text_stage") or "")
    if stage == CHUNK_ANALYSIS:
        start = int(extra.get("start_offset") or 0)
        end = int(extra.get("end_offset") or 0)
        content = source.content[start:end]
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if content_hash != extra.get("content_hash"):
            raise AppException("剧本分块内容已变化，请重新启动分析", code=40912, status_code=409)
        return render_system_prompt(
            "agent_source_chunk_analysis.md",
            chunk_number=str(int(extra.get("chunk_index") or 0) + 1),
            chunk_count=str(extra.get("chunk_count") or 1),
            start_offset=str(start),
            end_offset=str(end),
            input_text=content,
        )
    if stage == ASSET_ANALYSIS and uses_agent_workflow_v2(production):
        plan_record = await _stage_task_record(db, step, "episode_plan_task_record_id")
        if plan_record is None or plan_record.status != "success":
            raise AppException("分集规划尚未完成", code=40914, status_code=409)
        episode_plan = _validated_episode_plan_result(plan_record, step, source.content)
        return render_system_prompt(
            "agent_asset_analysis.md",
            episodes_json=json.dumps(
                _episode_asset_inputs(episode_plan, source.content),
                ensure_ascii=False,
            ),
            existing_assets_json=json.dumps(
                await _previous_assets(db, step),
                ensure_ascii=False,
            ),
        )
    if stage == GLOBAL_MERGE:
        entries = (step.extra or {}).get("chunks") or []
        records = await _load_task_records(db, [entry.get("task_record_id") for entry in entries])
        chunk_results = []
        for entry in entries:
            record = records.get(str(entry.get("task_record_id")))
            parsed = (record.extra or {}).get("parsed_result") if record else None
            if record is None or record.status != "success" or not isinstance(parsed, dict):
                raise AppException("剧本分块尚未全部解析完成", code=40913, status_code=409)
            chunk_results.append(
                {
                    "chunk": entry,
                    "analysis": parsed,
                }
            )
        return render_system_prompt(
            "agent_source_global_merge.md",
            chunk_results_json=json.dumps(chunk_results, ensure_ascii=False),
        )
    if stage == EPISODE_PLANNING:
        if uses_agent_workflow_v2(production):
            planning_input = json.dumps(
                _source_block_inputs(step, source.content),
                ensure_ascii=False,
            )
        else:
            merge_record = await _stage_task_record(db, step, "merge_task_record_id")
            global_analysis = (
                (merge_record.extra or {}).get("parsed_result")
                if merge_record
                else None
            )
            if (
                merge_record is None
                or merge_record.status != "success"
                or not isinstance(global_analysis, dict)
            ):
                raise AppException("全剧结构分析尚未完成", code=40914, status_code=409)
            planning_input = json.dumps(global_analysis, ensure_ascii=False)
        return render_system_prompt(
            "agent_episode_planning.md",
            source_blocks_json=planning_input,
        )
    raise AppException("未知的整剧文本任务类型", code=50043, status_code=500)


def _source_block_inputs(step: AgentStep, source_content: str) -> List[Dict[str, Any]]:
    _scope_text, scope_start, scope_end = _analysis_source_scope(step, source_content)
    result = []
    for entry in (step.extra or {}).get("chunks") or []:
        chunk_start = int(entry.get("start_offset") or 0)
        chunk_end = int(entry.get("end_offset") or 0)
        chunk_content = source_content[chunk_start:chunk_end]
        if (
            not 0 <= chunk_start < chunk_end <= len(source_content)
            or hashlib.sha256(chunk_content.encode("utf-8")).hexdigest()
            != entry.get("content_hash")
        ):
            raise AppException("剧本分块内容已变化，请重新启动分析", code=40912, status_code=409)
        start = max(chunk_start, scope_start)
        end = min(chunk_end, scope_end)
        if start >= end:
            continue
        result.append(
            {
                "block_id": int(entry.get("index") or 0),
                "source_start": start,
                "source_end": end,
                "content": source_content[start:end],
            }
        )
    return result


def _analysis_source_scope(
    step: AgentStep,
    source_content: str,
) -> tuple[str, int, int]:
    extra = step.extra or {}
    if extra.get("analysis_scope") != "incremental":
        return source_content, 0, len(source_content)
    start = int(extra.get("incremental_source_start") or 0)
    end = int(extra.get("incremental_source_end") or len(source_content))
    if not 0 <= start < end <= len(source_content):
        raise AppException("补充剧本原文范围无效", code=40912, status_code=409)
    return source_content[start:end], start, end


async def _merge_incremental_episode_plan(
    db: AsyncSession,
    production: AgentProduction,
    step: AgentStep,
    incremental_plan: Dict[str, Any],
) -> Dict[str, Any]:
    if (step.extra or {}).get("analysis_scope") != "incremental":
        return incremental_plan
    previous_step_id = (step.extra or {}).get("previous_source_step_id")
    previous_step = await db.get(AgentStep, _uuid(previous_step_id)) if previous_step_id else None
    previous_plan = (previous_step.extra or {}).get("episode_plan") if previous_step else None
    previous_episodes = [
        dict(item)
        for item in (previous_plan or {}).get("episodes") or []
        if isinstance(item, dict)
    ]
    if not previous_episodes:
        return incremental_plan

    chapter_result = await db.execute(
        select(ProjectChapter).where(
            ProjectChapter.project_id == production.project_id,
            ProjectChapter.user_id == production.user_id,
            ProjectChapter.is_enabled.is_(True),
            ProjectChapter.extra["agent_production_id"].as_string()
            == str(production.id),
        )
    )
    chapter_by_episode = {
        int((chapter.extra or {}).get("episode_number") or 0): chapter
        for chapter in chapter_result.scalars().all()
    }
    for index, item in enumerate(previous_episodes, start=1):
        item["episode_number"] = index
        chapter = chapter_by_episode.get(index)
        plan_id = (chapter.extra or {}).get("episode_plan_id") if chapter else None
        if plan_id:
            item["plan_id"] = str(plan_id)

    appended_episodes = [
        dict(item)
        for item in incremental_plan.get("episodes") or []
        if isinstance(item, dict)
    ]
    first_episode_number = len(previous_episodes) + 1
    for index, item in enumerate(appended_episodes, start=first_episode_number):
        item["episode_number"] = index
        item.pop("plan_id", None)
    step.extra = {
        **(step.extra or {}),
        "previous_episode_count": len(previous_episodes),
        "incremental_episode_count": len(appended_episodes),
        "incremental_episode_numbers": list(
            range(first_episode_number, first_episode_number + len(appended_episodes))
        ),
    }
    return {
        **(previous_plan or {}),
        **incremental_plan,
        "planning_summary": str(
            incremental_plan.get("planning_summary")
            or (previous_plan or {}).get("planning_summary")
            or ""
        ),
        "episodes": [*previous_episodes, *appended_episodes],
    }


def _episode_asset_inputs(
    episode_plan: Dict[str, Any],
    source_content: str,
) -> List[Dict[str, Any]]:
    result = []
    for episode in episode_plan.get("episodes") or []:
        start = int(episode["source_start"])
        end = int(episode["source_end"])
        result.append(
            {
                "episode_number": int(episode["episode_number"]),
                "title": episode["title"],
                "source_start": start,
                "source_end": end,
                "source_content": source_content[start:end],
            }
        )
    return result


async def _previous_assets(
    db: AsyncSession,
    step: AgentStep,
) -> Dict[str, Any]:
    previous_bible_id = (step.extra or {}).get("previous_bible_version_id")
    if not previous_bible_id:
        return {}
    previous_bible = await db.get(SeriesBibleVersion, _uuid(previous_bible_id))
    if previous_bible is None:
        return {}
    return {
        key: (previous_bible.content or {}).get(key) or []
        for key in (
            "characters",
            "character_variants",
            "scenes",
            "scene_variants",
            "props",
            "prop_variants",
        )
    }


async def settle_agent_text_task_cost(
    db: AsyncSession,
    task_record: UserTaskRecord,
    ai_model: AiModel,
    response_extra: Dict[str, Any],
) -> None:
    production_id = _uuid((task_record.extra or {}).get("agent_production_id"))
    production = await _lock_production(db, production_id)
    previous_points_cost = task_record.points_cost
    await settle_text_task_points(
        db,
        task_record,
        ai_model,
        response_extra,
        remark_prefix="整剧剧本分析",
    )
    if production is not None:
        production.consumed_points = max(
            0,
            production.consumed_points + task_record.points_cost - previous_points_cost,
        )


async def fail_agent_text_task(
    db: AsyncSession,
    task_record: UserTaskRecord,
    reason: str,
    *,
    raw_reason: Optional[str] = None,
    refund: bool = True,
) -> None:
    if task_record.status in {"success", "failed"}:
        return
    reason = sanitize_public_message(reason)
    extra = task_record.extra or {}
    production = await _lock_production(db, _uuid(extra.get("agent_production_id")))
    step = await _lock_step(
        db,
        _uuid(extra.get("agent_production_id")),
        _uuid(extra.get("agent_step_id")),
    )
    refund_transaction_id = extra.get("refund_transaction_id")
    if refund and task_record.points_cost > 0 and not refund_transaction_id:
        transaction = await change_user_points(
            db,
            user_id=task_record.user_id,
            amount=task_record.points_cost,
            transaction_type="refund",
            remark=f"任务失败退回积分：{task_record.title}",
            auto_commit=False,
        )
        refund_transaction_id = str(transaction.id)

    task_record.status = "failed"
    task_record.result = reason
    task_record.extra = {
        **extra,
        "failed_reason": reason,
        "raw_failed_reason": raw_reason or reason,
        "refund_transaction_id": refund_transaction_id,
    }
    if production is not None and refund_transaction_id:
        production.consumed_points = max(0, production.consumed_points - task_record.points_cost)
    if step is not None and step.status in {"queued", "running"}:
        step.status = transition_step_status(step.status, "failed")
        step.error_summary = reason
        step.finished_at = beijing_datetime()
    if production is not None and production.status in {"planning", "running"}:
        production.status = transition_production_status(production.status, "partially_failed")
        production.error_summary = reason
        production.lock_version += 1
        source = await db.get(ProjectSourceDocument, production.source_document_id)
        if (
            source is not None
            and source.project_id == production.project_id
            and source.user_id == production.user_id
        ):
            source.parse_status = "failed"
        db.add(
            AgentEvent(
                production_id=production.id,
                step_id=step.id if step else None,
                event_type="source_analysis.failed",
                source="worker",
                payload={"task_record_id": str(task_record.id), "reason": reason},
            )
        )
    await db.commit()


async def mark_source_analysis_blocked(
    db: AsyncSession,
    production_id: UUID,
    step_id: UUID,
    reason: str,
) -> None:
    production = await _lock_production(db, production_id)
    step = await _lock_step(db, production_id, step_id)
    if (
        production is None
        or step is None
        or production.status
        in {
            "waiting_approval",
            "cancelled",
            "completed",
        }
    ):
        return
    reason = sanitize_public_message(reason)
    previous_status = production.status
    if production.status in {"planning", "running", "partially_failed"}:
        production.status = transition_production_status(production.status, "paused")
        production.lock_version += 1
    production.error_summary = reason
    production.extra = {**(production.extra or {}), "paused_from_status": previous_status}
    if step.status == "failed":
        step.status = transition_step_status(step.status, "queued")
    step.extra = {**(step.extra or {}), "redispatch_requested": True}
    step.error_summary = reason
    db.add(
        AgentEvent(
            production_id=production.id,
            step_id=step.id,
            event_type="source_analysis.blocked",
            source="worker",
            payload={"reason": reason},
        )
    )
    await db.commit()


async def _create_agent_text_task(
    db: AsyncSession,
    production: AgentProduction,
    step: AgentStep,
    ai_model: AiModel,
    stage: str,
    *,
    title: str,
    role_extra: Dict[str, Any],
) -> UserTaskRecord:
    await ensure_model_minimum_balance(db, production.user_id, ai_model)
    points_cost = calculate_text_submission_points_cost(ai_model)
    if production.max_points is not None and production.consumed_points + points_cost > production.max_points:
        raise AppException("整剧任务已达到积分预算上限", code=40052, status_code=400)
    points_transaction = None
    if points_cost > 0:
        points_transaction = await consume_user_points(
            db,
            user_id=production.user_id,
            amount=points_cost,
            remark=f"{title}消耗积分",
            auto_commit=False,
        )
    record = await create_user_task_record(
        db,
        user_id=production.user_id,
        ai_model_id=ai_model.id,
        points_transaction_id=points_transaction.id if points_transaction else None,
        business_type="project",
        business_id=production.project_id,
        generation_type=_GENERATION_TYPES[stage],
        status="pending",
        title=title,
        prompt="系统提示词",
        points_cost=points_cost,
        extra={
            "agent_production_id": str(production.id),
            "agent_step_id": str(step.id),
            "agent_text_stage": stage,
            "prompt_source": "system",
            "dispatch_state": "pending",
            **role_extra,
        },
        # Stale expiration commits internally; this batch must remain atomic.
        expire_stale=False,
    )
    await db.flush()
    production.estimated_points += points_cost
    production.consumed_points += points_cost
    return record


async def _complete_source_analysis(
    db: AsyncSession,
    production: AgentProduction,
    step: AgentStep,
    source: ProjectSourceDocument,
    analysis_record: UserTaskRecord,
    plan_record: UserTaskRecord,
) -> None:
    analysis_result = (analysis_record.extra or {}).get("parsed_result") or {}
    episode_plan = (plan_record.extra or {}).get("parsed_result") or {}
    workflow_v2 = uses_agent_workflow_v2(production)
    bible = None
    candidates = []
    variants = []
    warnings = []
    if workflow_v2:
        bible, candidates, variants, warnings = await initialize_script_assets(
            db,
            production,
            step,
            analysis_result,
            episode_plan,
        )
    step.status = transition_step_status(step.status, "waiting_approval")
    step.progress_current = step.progress_total
    step.output_version = (step.output_version or 0) + 1
    step.finished_at = beijing_datetime()
    step.extra = {
        **(step.extra or {}),
        "global_analysis": analysis_result,
        **({"asset_analysis": analysis_result} if workflow_v2 else {}),
        "episode_plan": episode_plan,
        "script_warnings": warnings,
        **({"bible_version_id": str(bible.id)} if bible is not None else {}),
    }
    source.parse_status = "success"
    source.extra = {
        **(source.extra or {}),
        "agent_production_id": str(production.id),
        "source_analysis_step_id": str(step.id),
    }
    production.status = transition_production_status(production.status, "waiting_approval")
    production.current_stage = "script_review" if workflow_v2 else "episode_plan_review"
    production.error_summary = None
    production.lock_version += 1
    checkpoint_type = "script_review" if workflow_v2 else "episode_plan_review"
    checkpoint_result = await db.execute(
        select(AgentCheckpoint).where(
            AgentCheckpoint.step_id == step.id,
            AgentCheckpoint.checkpoint_type == checkpoint_type,
        )
    )
    if checkpoint_result.scalar_one_or_none() is None:
        episodes = episode_plan.get("episodes") if isinstance(episode_plan, dict) else []
        db.add(
            AgentCheckpoint(
                production_id=production.id,
                step_id=step.id,
                checkpoint_type=checkpoint_type,
                status="pending",
                summary=(
                    f"剧本处理完成，已生成 {len(episodes or [])} 集、"
                    f"{len(candidates)} 个基础资产和 {len(variants)} 个资产变体，请审核。"
                    if workflow_v2
                    else f"全剧解析完成，已生成 {len(episodes or [])} 集规划，请审核后继续。"
                ),
                impact={
                    "next_stage": "core_assets" if workflow_v2 else "asset_extraction",
                    "episode_count": len(episodes or []),
                    **(
                        {
                            "base_asset_count": len(candidates),
                            "asset_variant_count": len(variants),
                            "warning_count": len(warnings),
                        }
                        if workflow_v2
                        else {}
                    ),
                },
                extra={
                    "episode_plan_task_record_id": str(plan_record.id),
                    **(
                        {"bible_version_id": str(bible.id)}
                        if bible is not None
                        else {}
                    ),
                },
            )
        )
    db.add(
        AgentEvent(
            production_id=production.id,
            step_id=step.id,
            event_type="source_analysis.completed",
            source="worker",
            payload={
                (
                    "asset_analysis_task_record_id"
                    if workflow_v2
                    else "merge_task_record_id"
                ): str(analysis_record.id),
                "episode_plan_task_record_id": str(plan_record.id),
            },
        )
    )


async def _get_text_model(db: AsyncSession, production: AgentProduction) -> AiModel:
    model_id = _uuid((production.production_spec or {}).get("text_model_id"))
    result = await db.execute(
        select(AiModel).where(
            AiModel.id == model_id,
            AiModel.model_type == "text",
            AiModel.is_enabled.is_(True),
        )
    )
    model = result.scalar_one_or_none()
    if model is None:
        raise AppException("文本模型不存在或已禁用", code=40404, status_code=404)
    return model


async def _lock_production(db: AsyncSession, production_id: UUID) -> Optional[AgentProduction]:
    result = await db.execute(
        select(AgentProduction).where(AgentProduction.id == production_id).with_for_update(of=AgentProduction)
    )
    return result.scalar_one_or_none()


async def _lock_step(
    db: AsyncSession,
    production_id: UUID,
    step_id: UUID,
) -> Optional[AgentStep]:
    result = await db.execute(
        select(AgentStep)
        .where(AgentStep.id == step_id, AgentStep.production_id == production_id)
        .with_for_update(of=AgentStep)
    )
    return result.scalar_one_or_none()


def _chunk_entries(step: AgentStep, chunks: List[SourceTextChunk]) -> List[Dict[str, Any]]:
    existing = {
        int(item.get("index")): item
        for item in ((step.extra or {}).get("chunks") or [])
        if isinstance(item, dict) and str(item.get("index", "")).isdigit()
    }
    entries = []
    for chunk in chunks:
        old = existing.get(chunk.index) or {}
        entry = {
            "index": chunk.index,
            "start_offset": chunk.start_offset,
            "end_offset": chunk.end_offset,
            "content_hash": chunk.content_hash,
        }
        if old.get("content_hash") == chunk.content_hash and old.get("task_record_id"):
            entry["task_record_id"] = old["task_record_id"]
        entries.append(entry)
    return entries


async def _load_task_records(
    db: AsyncSession,
    raw_ids: List[Any],
) -> Dict[str, UserTaskRecord]:
    ids = [_uuid(value) for value in raw_ids if value]
    if not ids:
        return {}
    result = await db.execute(select(UserTaskRecord).where(UserTaskRecord.id.in_(ids)))
    return {str(record.id): record for record in result.scalars().all()}


async def _stage_task_record(
    db: AsyncSession,
    step: AgentStep,
    key: str,
) -> Optional[UserTaskRecord]:
    task_id = (step.extra or {}).get(key)
    return await db.get(UserTaskRecord, _uuid(task_id)) if task_id else None


def _undispatched_records(records: List[Optional[UserTaskRecord]]) -> List[UserTaskRecord]:
    return [record for record in records if record is not None and _is_undispatched(record)]


def _is_undispatched(record: UserTaskRecord) -> bool:
    return record.status == "pending" and (record.extra or {}).get("dispatch_state") != "dispatched"


def _uuid(value: Any) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise AppException("整剧任务关联标识无效", code=50044, status_code=500) from exc
