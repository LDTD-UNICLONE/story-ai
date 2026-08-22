from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict, List, Literal, Optional, Tuple
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import AppException
from app.core.timezone import beijing_datetime
from app.models.agent_core_asset import AgentCoreAssetLock
from app.models.agent_production import AgentEvent, AgentProduction, AgentStep
from app.models.ai_model import AiModel
from app.models.project import Project
from app.models.project_chapter import ProjectChapter
from app.models.project_generated_asset import ProjectGeneratedAsset
from app.models.project_storyboard import ProjectStoryboard
from app.models.task_record import UserTaskRecord
from app.models.user import User
from app.schemas.agent_batch_production import (
    AgentBatchDispatchRequest,
    AgentVideoModelSelectionRequest,
)
from app.schemas.agent_production_control import AgentJobRetryRequest, AgentJobSkipRequest
from app.schemas.project_storyboard import (
    ProjectStoryboardAnalyzeRequest,
    ProjectStoryboardImageGenerateRequest,
    ProjectStoryboardVideoGenerateRequest,
)
from app.services.agent_production_state import (
    transition_production_status,
    transition_step_status,
)
from app.services.agent_storyboard_bindings import (
    AGENT_ASSET_BINDING_VERSION,
    bind_storyboards_to_core_lock,
    storyboard_asset_ids,
    storyboard_estimated_duration_seconds,
    storyboard_quality_issues,
)
from app.services.agent_default_models import get_agent_video_model
from app.services.agent_task_context import build_agent_task_context
from app.services.agent_workflow import agent_pilot_episode_count, uses_agent_workflow_v2
from app.services.model_points import (
    calculate_submission_points_cost,
    model_minimum_balance_points,
)
from app.services.points import ensure_user_points_enough
from app.services.project_storyboard_images import submit_storyboard_image_generation
from app.services.project_storyboard_videos import submit_storyboard_video_generation
from app.services.project_storyboards import (
    reconcile_storyboard_media_tasks,
    submit_storyboard_analysis,
)


BatchPhase = Literal["storyboards", "images", "videos"]
ACTIVE_STATUSES = {"pending", "running"}
SUCCESS_STATUSES = {"success", "selected", "skipped"}


@dataclass
class BatchContext:
    production: AgentProduction
    project: Project
    core_lock: AgentCoreAssetLock
    step: Optional[AgentStep]
    chapters: List[ProjectChapter]
    storyboards: List[ProjectStoryboard]


async def get_batch_production(
    db: AsyncSession,
    production_id: UUID,
    user_id: UUID,
) -> Dict[str, Any]:
    context = await _get_context(db, production_id, user_id, lock=True)
    before = _state_signature(context)
    await _reconcile(db, context)
    if _state_signature(context) != before:
        context.production.lock_version += 1
    await db.commit()
    return await _status(db, context)


async def select_batch_video_model(
    db: AsyncSession,
    production_id: UUID,
    user: User,
    payload: AgentVideoModelSelectionRequest,
) -> Dict[str, Any]:
    context = await _get_context(db, production_id, user.id, lock=True)
    if uses_agent_workflow_v2(context.production):
        raise AppException(
            "当前 Agent 视频仅支持按分集选择模型并生成",
            code=40986,
            status_code=409,
            data={
                "episode_videos_endpoint": (
                    f"/api/v1/agent-productions/{production_id}"
                    "/episodes/{chapter_id}/video-generations"
                )
            },
        )
    _check_core_lock_version(context, payload.expected_core_asset_lock_version)
    if context.production.status in {"completed", "cancelled"}:
        raise AppException("当前整剧状态不允许选择视频模型", code=40966, status_code=409)
    if _phase(context) != "videos":
        raise AppException("尚未进入视频生成阶段", code=40966, status_code=409)

    current_id = _optional_uuid((context.production.production_spec or {}).get("video_model_id"))
    current_resolution = str(
        (context.production.production_spec or {}).get("video_resolution") or "720p"
    )
    if current_id == payload.video_model_id and current_resolution == payload.video_resolution:
        await db.commit()
        return await _status(db, context)
    if context.step is not None and (
        any(_attempts(context.step, "videos").values())
        or bool((context.step.extra or {}).get("video_task_ids"))
    ):
        raise AppException(
            "视频生成已经开始，不能更换模型",
            code=40967,
            status_code=409,
        )

    model = await get_agent_video_model(db, payload.video_model_id)
    context.production.production_spec = {
        **(context.production.production_spec or {}),
        "video_model_id": str(model.id),
        "video_resolution": payload.video_resolution,
    }
    context.production.lock_version += 1
    db.add(
        AgentEvent(
            production_id=context.production.id,
            step_id=context.step.id if context.step else None,
            actor_user_id=user.id,
            event_type="batch.video_model_selected",
            source="user",
            payload={
                "video_model_id": str(model.id),
                "model_id": model.model_id,
                "video_resolution": payload.video_resolution,
                "core_asset_lock_version": context.core_lock.version,
            },
        )
    )
    await db.commit()
    return await _status(db, context)


async def dispatch_batch_production(
    db: AsyncSession,
    production_id: UUID,
    user: User,
    payload: AgentBatchDispatchRequest,
    *,
    dispatch_source: str = "user",
    scope_ids: Optional[List[UUID]] = None,
    replace_storyboard_scope: bool = False,
) -> Dict[str, Any]:
    context = await _get_context(db, production_id, user.id, lock=True)
    _check_core_lock_version(context, payload.expected_core_asset_lock_version)
    _assert_dispatchable_production(context.production)
    await _ensure_pilot_completed(db, context)
    if context.step is not None and payload.idempotency_key in set(
        (context.step.extra or {}).get("dispatch_idempotency_keys") or []
    ):
        await _reconcile(db, context)
        await db.commit()
        return await _status(db, context)
    if context.step is None:
        if context.production.current_stage != "batch_production":
            raise AppException("当前整剧阶段尚未进入批量生产", code=40951, status_code=409)
        context.step = await _create_batch_step(db, context, user.id, payload.idempotency_key)
        context.production.current_stage = "batch_storyboards"
        _move_production_status(context.production, "running")
        context.production.lock_version += 1
        await db.commit()

    if replace_storyboard_scope:
        step_extra = dict(context.step.extra or {})
        if scope_ids is None:
            step_extra.pop("storyboard_scope_ids", None)
        else:
            existing_scope = step_extra.get("storyboard_scope_ids")
            persisted_scope = {
                str(value)
                for value in (existing_scope if isinstance(existing_scope, list) else [])
                if _optional_uuid(value) is not None
            }
            persisted_scope.update(str(value) for value in scope_ids)
            step_extra["storyboard_scope_ids"] = sorted(persisted_scope)
        context.step.extra = step_extra
        if _phase(context) != "storyboards":
            _set_phase(context.step, "storyboards")
            context.step.finished_at = None
            context.production.current_stage = "batch_storyboards"
            context.production.error_summary = None
            if context.step.status == "failed":
                context.step.status = transition_step_status(context.step.status, "queued")
            elif context.step.status == "completed":
                context.step.status = transition_step_status(
                    context.step.status,
                    "invalidated",
                )
                context.step.status = transition_step_status(context.step.status, "queued")

    await _reconcile(db, context)
    phase = _phase(context)
    if uses_agent_workflow_v2(context.production) and phase == "episode_videos":
        await db.commit()
        raise AppException(
            "当前 Agent 视频必须按分集生成，不能使用全剧批量调度",
            code=40986,
            status_code=409,
            data={
                "episode_videos_endpoint": (
                    f"/api/v1/agent-productions/{production_id}"
                    "/episodes/{chapter_id}/video-generations"
                )
            },
        )
    if phase == "exceptions":
        await _try_reopen_exceptions(db, context)
        phase = _phase(context)
        if phase == "exceptions" and context.production.status != "partially_failed":
            _move_production_status(context.production, "partially_failed")
            context.production.current_stage = "batch_exceptions"
    if phase in {"completed", "exceptions", "completion_pending"}:
        await db.commit()
        return await _status(db, context)
    if context.production.status == "paused":
        raise AppException("整剧任务已暂停，不再创建新任务", code=40952, status_code=409)

    batch_phase: BatchPhase = phase  # type: ignore[assignment]
    available_slots = await _available_task_slots(db, user.id, batch_phase)
    selected = _scoped_eligible_scope_ids(context, batch_phase, scope_ids)[
        : min(payload.max_tasks, available_slots)
    ]
    model = await _phase_model(db, context.production, batch_phase)
    estimated_cost = sum(
        _scope_submission_cost(context, model, batch_phase, scope_id) for scope_id in selected
    )
    await _ensure_budget(db, context.production, user.id, model, estimated_cost)

    _append_idempotency_key(context.step, payload.idempotency_key)
    context.step.extra = {
        **(context.step.extra or {}),
        "last_dispatch_max_tasks": payload.max_tasks,
    }
    _move_step_status(context.step, "running")
    _move_production_status(context.production, "running")
    context.production.current_stage = f"batch_{batch_phase}"
    context.production.lock_version += 1
    db.add(
        AgentEvent(
            production_id=context.production.id,
            step_id=context.step.id,
            actor_user_id=user.id,
            event_type="batch.dispatched",
            source=dispatch_source,
            payload={
                "phase": batch_phase,
                "scope_count": len(selected),
                "available_slots": available_slots,
                "idempotency_key": payload.idempotency_key,
            },
        )
    )
    await db.commit()

    for scope_id in selected:
        context = await _get_context(db, production_id, user.id, lock=True)
        if context.production.status == "paused":
            await db.rollback()
            break
        if _phase(context) != batch_phase or scope_id not in _eligible_scope_ids(
            context, batch_phase
        ):
            await db.rollback()
            continue
        model = await _phase_model(db, context.production, batch_phase)
        try:
            task_record, points_cost = await _submit_scope(
                db,
                context,
                user,
                batch_phase,
                scope_id,
                model,
            )
        except AppException as exc:
            await db.rollback()
            if exc.code == 42920:
                break
            await _record_dispatch_error(
                db,
                production_id,
                context.step.id,
                batch_phase,
                scope_id,
                exc.message,
            )
            context = await _get_context(db, production_id, user.id, lock=True)
            model = await _phase_model(db, context.production, batch_phase)
            continue
        await _record_submission(
            db,
            production_id,
            context.step.id,
            batch_phase,
            scope_id,
            task_record.id,
            points_cost if task_record.status != "failed" else 0,
        )

    return await _refresh_status(db, production_id, user.id)


async def resume_batch_production(
    db: AsyncSession,
    production_id: UUID,
    user: User,
) -> Dict[str, Any]:
    context = await _get_context(db, production_id, user.id, lock=True)
    if context.step is None:
        raise AppException("批量生产尚未开始", code=40951, status_code=409)
    max_tasks = max(1, settings.user_pending_media_task_limit or 5)
    return await dispatch_batch_production(
        db,
        production_id,
        user,
        AgentBatchDispatchRequest(
            expected_core_asset_lock_version=context.core_lock.version,
            idempotency_key=f"batch-resume-{context.production.lock_version}",
            max_tasks=min(max_tasks, 50),
        ),
    )


async def retry_batch_jobs(
    db: AsyncSession,
    production_id: UUID,
    user: User,
    payload: AgentJobRetryRequest,
) -> Dict[str, Any]:
    context = await _get_context(db, production_id, user.id, lock=True)
    _check_core_lock_version(context, payload.expected_core_asset_lock_version)
    replay = _job_action_replay(context.step, payload.idempotency_key)
    if replay is not None:
        return {**replay, "idempotent": True}
    _assert_job_action_allowed(context)
    phase = _job_phase(payload.stage)
    scope_ids = _validated_retry_scope_ids(context, phase, payload)
    available_slots = await _available_task_slots(db, user.id, phase)
    if available_slots < len(scope_ids):
        raise AppException(
            f"当前队列仅可再提交 {available_slots} 个任务",
            code=42920,
            status_code=429,
        )
    model = await _phase_model(db, context.production, phase)
    estimated_cost = sum(
        _scope_submission_cost(context, model, phase, scope_id) for scope_id in scope_ids
    )
    await _account_refunded_tasks(db, context)
    await _ensure_budget(db, context.production, user.id, model, estimated_cost)
    _prepare_manual_job_action(context, phase, payload.idempotency_key, "retry")
    await db.commit()

    task_ids: List[UUID] = []
    affected_scope_ids: List[UUID] = []
    points_cost = 0
    for scope_id in scope_ids:
        context = await _get_context(db, production_id, user.id, lock=True)
        if context.production.status == "paused":
            await db.rollback()
            break
        model = await _phase_model(db, context.production, phase)
        try:
            task_record, submitted_points = await _submit_scope(
                db,
                context,
                user,
                phase,
                scope_id,
                model,
            )
        except AppException:
            await db.rollback()
            continue
        charged_points = submitted_points if task_record.status != "failed" else 0
        await _record_submission(
            db,
            production_id,
            context.step.id,
            phase,
            scope_id,
            task_record.id,
            charged_points,
        )
        task_ids.append(task_record.id)
        affected_scope_ids.append(scope_id)
        points_cost += charged_points
    result = {
        "action": "retry",
        "stage": payload.stage,
        "requested_count": len(scope_ids),
        "affected_count": len(affected_scope_ids),
        "task_record_ids": task_ids,
        "affected_scope_ids": affected_scope_ids,
        "points_cost": points_cost,
        "idempotent": False,
    }
    await _complete_job_action(
        db,
        production_id,
        user.id,
        payload.idempotency_key,
        result,
    )
    await _refresh_status(db, production_id, user.id)
    return result


async def skip_batch_jobs(
    db: AsyncSession,
    production_id: UUID,
    user: User,
    payload: AgentJobSkipRequest,
) -> Dict[str, Any]:
    context = await _get_context(db, production_id, user.id, lock=True)
    _check_core_lock_version(context, payload.expected_core_asset_lock_version)
    replay = _job_action_replay(context.step, payload.idempotency_key)
    if replay is not None:
        return {**replay, "idempotent": True}
    _assert_job_action_allowed(context)
    phase = _job_phase(payload.stage)
    scope_ids = _validated_skip_scope_ids(context, phase, payload.scope_ids)
    _prepare_manual_job_action(context, phase, payload.idempotency_key, "skip")
    now = beijing_datetime().isoformat()
    for scope_id in scope_ids:
        if phase == "storyboards":
            chapter = next(item for item in context.chapters if item.id == scope_id)
            _mark_scope_manually_resolved(
                chapter,
                "storyboard",
                payload.reason,
                payload.replacement_url,
                now,
            )
            chapter.extra = {
                **(chapter.extra or {}),
                "storyboard_analysis_status": "skipped",
            }
            continue
        storyboard = next(item for item in context.storyboards if item.id == scope_id)
        await _resolve_storyboard_manually(
            db,
            context,
            storyboard,
            payload.stage,
            payload.reason,
            payload.replacement_url,
            now,
        )
        _remove_blocked_storyboard(context.step, storyboard.id)
    result = {
        "action": "skip",
        "stage": payload.stage,
        "requested_count": len(scope_ids),
        "affected_count": len(scope_ids),
        "task_record_ids": [],
        "affected_scope_ids": scope_ids,
        "points_cost": 0,
        "idempotent": False,
    }
    _store_job_action_result(
        context.step,
        payload.idempotency_key,
        result,
    )
    db.add(
        AgentEvent(
            production_id=context.production.id,
            step_id=context.step.id,
            actor_user_id=user.id,
            event_type="batch.jobs_skipped",
            source="user",
            payload={
                "stage": payload.stage,
                "scope_ids": [str(value) for value in scope_ids],
                "reason": payload.reason,
                "replacement_url": payload.replacement_url,
                "idempotency_key": payload.idempotency_key,
            },
        )
    )
    await db.commit()
    await _refresh_status(db, production_id, user.id)
    return result


async def _get_context(
    db: AsyncSession,
    production_id: UUID,
    user_id: UUID,
    *,
    lock: bool,
) -> BatchContext:
    production_query = (
        select(AgentProduction, Project)
        .join(Project, Project.id == AgentProduction.project_id)
        .where(
            AgentProduction.id == production_id,
            AgentProduction.user_id == user_id,
            Project.user_id == user_id,
            Project.is_enabled.is_(True),
        )
        .execution_options(populate_existing=True)
    )
    if lock:
        production_query = production_query.with_for_update(of=AgentProduction)
    production_result = await db.execute(production_query)
    row = production_result.one_or_none()
    if row is None:
        raise AppException("整剧任务不存在", code=40430, status_code=404)
    production, project = row
    core_lock_result = await db.execute(
        select(AgentCoreAssetLock)
        .where(
            AgentCoreAssetLock.production_id == production.id,
            AgentCoreAssetLock.status == "active",
        )
        .order_by(AgentCoreAssetLock.version.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )
    core_lock = core_lock_result.scalar_one_or_none()
    if core_lock is None:
        raise AppException("核心资产尚未锁定", code=40950, status_code=409)

    pilot_count = agent_pilot_episode_count(production)
    chapter_result = await db.execute(
        select(ProjectChapter)
        .where(
            ProjectChapter.project_id == production.project_id,
            ProjectChapter.user_id == production.user_id,
            ProjectChapter.is_enabled.is_(True),
            ProjectChapter.extra["agent_production_id"].as_string() == str(production.id),
        )
        .order_by(ProjectChapter.sort_order.asc(), ProjectChapter.created_at.asc())
        .execution_options(populate_existing=True)
    )
    chapters = list(chapter_result.scalars().all())[pilot_count:]
    step_result = await db.execute(
        select(AgentStep)
        .where(
            AgentStep.production_id == production.id,
            AgentStep.stage == "batch_production",
            AgentStep.scope_type == "production",
            AgentStep.scope_id == production.id,
            AgentStep.input_version == core_lock.version,
        )
        .order_by(AgentStep.created_at.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )
    step = step_result.scalar_one_or_none()
    storyboards: List[ProjectStoryboard] = []
    if chapters:
        storyboard_result = await db.execute(
            select(ProjectStoryboard)
            .where(
                ProjectStoryboard.project_id == production.project_id,
                ProjectStoryboard.user_id == production.user_id,
                ProjectStoryboard.chapter_id.in_([chapter.id for chapter in chapters]),
                ProjectStoryboard.is_enabled.is_(True),
            )
            .order_by(
                ProjectStoryboard.chapter_id,
                ProjectStoryboard.shot_number,
                ProjectStoryboard.created_at,
            )
            .execution_options(populate_existing=True)
        )
        storyboards = list(storyboard_result.scalars().all())
    return BatchContext(production, project, core_lock, step, chapters, storyboards)


async def _ensure_pilot_completed(db: AsyncSession, context: BatchContext) -> None:
    if agent_pilot_episode_count(context.production) == 0:
        return
    result = await db.execute(
        select(AgentStep.id).where(
            AgentStep.production_id == context.production.id,
            AgentStep.stage == "pilot_production",
            AgentStep.input_version == context.core_lock.version,
            AgentStep.status == "completed",
        )
    )
    if result.scalar_one_or_none() is None:
        raise AppException("试播集尚未确认，不能开始批量生产", code=40950, status_code=409)


async def _create_batch_step(
    db: AsyncSession,
    context: BatchContext,
    user_id: UUID,
    idempotency_key: str,
) -> AgentStep:
    now = beijing_datetime()
    step = AgentStep(
        production_id=context.production.id,
        stage="batch_production",
        scope_type="production",
        scope_id=context.production.id,
        status="running",
        input_version=context.core_lock.version,
        progress_current=0,
        progress_total=len(context.chapters),
        attempt_count=1,
        started_at=now,
        extra={
            "phase": "storyboards",
            "chapter_ids": [str(chapter.id) for chapter in context.chapters],
            "core_asset_lock_id": str(context.core_lock.id),
            "core_asset_lock_version": context.core_lock.version,
            "storyboard_task_ids": {},
            "image_task_ids": {},
            "video_task_ids": {},
            "storyboard_attempts": {},
            "image_attempts": {},
            "video_attempts": {},
            "dispatch_idempotency_keys": [idempotency_key],
            "submitted_points": 0,
            "quality_issues": [],
            "blocked_storyboard_ids": [],
        },
    )
    db.add(step)
    await db.flush()
    db.add(
        AgentEvent(
            production_id=context.production.id,
            step_id=step.id,
            actor_user_id=user_id,
            event_type="batch.started",
            source="user",
            payload={
                "chapter_ids": [str(chapter.id) for chapter in context.chapters],
                "core_asset_lock_version": context.core_lock.version,
                "idempotency_key": idempotency_key,
            },
        )
    )
    return step


async def _reconcile(db: AsyncSession, context: BatchContext) -> None:
    step = context.step
    phase = _phase(context)
    if step is None or phase == "completed":
        return
    if context.production.current_stage == "core_asset_change_review":
        return
    await _account_refunded_tasks(db, context)
    if uses_agent_workflow_v2(context.production) and phase in {"images", "videos"}:
        _set_phase(step, "episode_videos")
        context.production.current_stage = "episode_videos"
        return
    if phase in {"images", "videos", "exceptions"} and any(
        int((storyboard.extra or {}).get("agent_asset_binding_version") or 0)
        < AGENT_ASSET_BINDING_VERSION
        for storyboard in context.storyboards
    ):
        await _refresh_storyboard_quality(db, context)
    if phase == "exceptions":
        return
    if phase in {"images", "videos"}:
        for storyboard in context.storyboards:
            await reconcile_storyboard_media_tasks(db, storyboard)
    step.progress_current = _completed_episode_count(context)

    for _ in range(4):
        phase = _phase(context)
        if phase == "completion_pending":
            if context.production.status != "paused":
                _finish_batch(context)
            return
        if phase == "storyboards":
            if _phase_has_work(context, "storyboards"):
                return
            scoped_chapters = _storyboard_scope_chapters(context)
            failed_chapters = [
                chapter
                for chapter in scoped_chapters
                if _chapter_status(chapter) == "failed"
            ]
            if failed_chapters:
                _set_phase(step, "exceptions")
                _move_step_status(step, "failed")
                _move_production_status(context.production, "partially_failed")
                context.production.current_stage = "batch_exceptions"
                context.production.error_summary = (
                    f"有 {len(failed_chapters)} 集分镜分析失败，请处理失败集"
                )
                return
            if any(
                _chapter_status(chapter) not in SUCCESS_STATUSES
                for chapter in scoped_chapters
            ):
                return
            await _refresh_storyboard_quality(db, context)
            if uses_agent_workflow_v2(context.production):
                _set_phase(step, "episode_videos")
                context.production.current_stage = "episode_videos"
                return
            step.extra = {
                **(step.extra or {}),
                "phase": "images",
            }
            context.production.current_stage = "batch_images"
            continue
        if phase == "images":
            if _phase_has_work(context, "images"):
                return
            _set_phase(step, "videos")
            context.production.current_stage = "batch_videos"
            continue
        if phase == "videos":
            if _phase_has_work(context, "videos"):
                return
            if context.production.status == "paused":
                _set_phase(step, "completion_pending")
                context.production.current_stage = "batch_completion_pending"
                return
            _finish_batch(context)
            return
        return


async def _refresh_storyboard_quality(db: AsyncSession, context: BatchContext) -> None:
    if context.step is None:
        return
    await bind_storyboards_to_core_lock(
        db,
        context.production,
        context.core_lock,
        context.storyboards,
    )
    issues = _batch_quality_issues(context)
    blocked_ids = sorted(
        {
            str(item.get("storyboard_id"))
            for item in issues
            if item.get("severity") == "error" and item.get("storyboard_id")
        }
    )
    context.step.extra = {
        **(context.step.extra or {}),
        "quality_issues": issues,
        "blocked_storyboard_ids": blocked_ids,
    }


async def _try_reopen_exceptions(db: AsyncSession, context: BatchContext) -> None:
    step = context.step
    if step is None or _phase(context) != "exceptions":
        return
    await _refresh_storyboard_quality(db, context)
    issues = list((step.extra or {}).get("quality_issues") or [])
    if any(item.get("severity") == "error" for item in issues):
        return
    if any(_chapter_status(chapter) not in SUCCESS_STATUSES for chapter in context.chapters):
        return

    _set_phase(step, "images")
    if _phase_has_work(context, "images"):
        _reopen_batch_phase(context, "images")
        return
    if any(
        _media_status(storyboard, "image") not in SUCCESS_STATUSES
        for storyboard in context.storyboards
    ):
        _set_phase(step, "exceptions")
        return

    _set_phase(step, "videos")
    if _phase_has_work(context, "videos"):
        _reopen_batch_phase(context, "videos")
        return
    if any(
        _media_status(storyboard, "video") not in SUCCESS_STATUSES
        for storyboard in context.storyboards
    ):
        _set_phase(step, "exceptions")
        return
    _finish_batch(context)


def _reopen_batch_phase(context: BatchContext, phase: BatchPhase) -> None:
    if context.step is None:
        return
    context.step.finished_at = None
    context.production.error_summary = None
    _move_production_status(context.production, "running")
    context.production.current_stage = f"batch_{phase}"


def _phase_has_work(context: BatchContext, phase: BatchPhase) -> bool:
    if _active_scope_count(context, phase):
        return True
    return bool(_eligible_scope_ids(context, phase))


def _finish_batch(context: BatchContext) -> None:
    if context.step is None:
        return
    failures = _failed_item_count(context)
    context.step.finished_at = beijing_datetime()
    context.step.progress_current = _completed_episode_count(context)
    if failures:
        _set_phase(context.step, "exceptions")
        _move_step_status(context.step, "failed")
        _move_production_status(context.production, "partially_failed")
        context.production.current_stage = "batch_exceptions"
        context.production.error_summary = f"批量生产有 {failures} 个失败或阻断项"
    else:
        _set_phase(context.step, "completed")
        _move_step_status(context.step, "running")
        _move_step_status(context.step, "completed")
        _move_production_status(context.production, "completed")
        context.production.current_stage = "completed"
        context.production.error_summary = None


def _eligible_scope_ids(context: BatchContext, phase: BatchPhase) -> List[UUID]:
    if context.step is None:
        return []
    max_attempts = _max_attempts(context.production)
    attempts = _attempts(context.step, phase)
    result: List[UUID] = []
    if phase == "storyboards":
        for chapter in _storyboard_scope_chapters(context):
            status = _chapter_status(chapter)
            if status in ACTIVE_STATUSES | SUCCESS_STATUSES or status == "failed":
                continue
            if status == "invalidated" or attempts.get(str(chapter.id), 0) < max_attempts:
                result.append(chapter.id)
        return result

    blocked = set((context.step.extra or {}).get("blocked_storyboard_ids") or [])
    for storyboard in context.storyboards:
        if str(storyboard.id) in blocked:
            continue
        if phase == "videos" and _media_status(storyboard, "image") not in SUCCESS_STATUSES:
            continue
        status = _media_status(storyboard, "image" if phase == "images" else "video")
        if status not in ACTIVE_STATUSES | SUCCESS_STATUSES and (
            status == "invalidated" or attempts.get(str(storyboard.id), 0) < max_attempts
        ):
            result.append(storyboard.id)
    return result


def _scoped_eligible_scope_ids(
    context: BatchContext,
    phase: BatchPhase,
    scope_ids: Optional[List[UUID]],
) -> List[UUID]:
    eligible = _eligible_scope_ids(context, phase)
    if scope_ids is None:
        return eligible
    allowed = set(scope_ids)
    return [scope_id for scope_id in eligible if scope_id in allowed]


def _storyboard_scope_chapters(context: BatchContext) -> List[ProjectChapter]:
    raw_scope = (context.step.extra or {}).get("storyboard_scope_ids") if context.step else None
    if not isinstance(raw_scope, list):
        return context.chapters
    scope_ids = {
        value
        for raw in raw_scope
        if (value := _optional_uuid(raw)) is not None
    }
    return [chapter for chapter in context.chapters if chapter.id in scope_ids]


def _active_scope_count(context: BatchContext, phase: BatchPhase) -> int:
    if phase == "storyboards":
        return sum(_chapter_status(chapter) in ACTIVE_STATUSES for chapter in context.chapters)
    kind = "image" if phase == "images" else "video"
    blocked = (
        set((context.step.extra or {}).get("blocked_storyboard_ids") or [])
        if context.step
        else set()
    )
    return sum(
        str(storyboard.id) not in blocked and _media_status(storyboard, kind) in ACTIVE_STATUSES
        for storyboard in context.storyboards
    )


async def _submit_scope(
    db: AsyncSession,
    context: BatchContext,
    user: User,
    phase: BatchPhase,
    scope_id: UUID,
    model: AiModel,
) -> Tuple[UserTaskRecord, int]:
    if phase == "storyboards":
        return await submit_storyboard_analysis(
            db,
            project_id=context.production.project_id,
            chapter_id=scope_id,
            user=user,
            payload=ProjectStoryboardAnalyzeRequest(ai_model_id=model.id),
            agent_context=_agent_task_context(context, phase, scope_id),
        )
    storyboard = next(item for item in context.storyboards if item.id == scope_id)
    asset_ids = storyboard_asset_ids(storyboard)
    if phase == "images":
        return await submit_storyboard_image_generation(
            db,
            project_id=context.production.project_id,
            chapter_id=storyboard.chapter_id,
            storyboard_id=storyboard.id,
            user=user,
            payload=ProjectStoryboardImageGenerateRequest(
                ai_model_id=model.id,
                aspect_ratio=_image_aspect_ratio(context.project.generation_ratio),
                character_ids=asset_ids["character"],
                scene_ids=asset_ids["scene"],
                prop_ids=asset_ids["prop"],
            ),
            agent_context=_agent_task_context(context, phase, scope_id),
        )
    return await submit_storyboard_video_generation(
        db,
        project_id=context.production.project_id,
        chapter_id=storyboard.chapter_id,
        storyboard_id=storyboard.id,
        user=user,
        payload=ProjectStoryboardVideoGenerateRequest(
            ai_model_id=model.id,
            generation_mode="reference",
            resolution=str(
                (context.production.production_spec or {}).get("video_resolution") or "720p"
            ),
            return_last_frame=True,
            character_ids=asset_ids["character"],
            scene_ids=asset_ids["scene"],
            prop_ids=asset_ids["prop"],
            uploaded_images=[_storyboard_image_url(storyboard)],
            extra={
                "duration_seconds": storyboard_estimated_duration_seconds(storyboard),
                "generate_audio": bool(
                    (context.production.production_spec or {}).get("generate_audio", False)
                ),
            },
        ),
        agent_context=_agent_task_context(context, phase, scope_id),
    )


def _agent_task_context(
    context: BatchContext,
    phase: BatchPhase,
    scope_id: UUID,
) -> Dict[str, object]:
    if context.step is None:
        raise AppException("批量生产步骤不存在", code=40951, status_code=409)
    return build_agent_task_context(
        production_id=context.production.id,
        step_id=context.step.id,
        stage=f"batch_{phase}",
        scope_type="chapter" if phase == "storyboards" else "storyboard",
        scope_id=scope_id,
        attempt_number=_attempts(context.step, phase).get(str(scope_id), 0) + 1,
    )


async def _record_submission(
    db: AsyncSession,
    production_id: UUID,
    step_id: UUID,
    phase: BatchPhase,
    scope_id: UUID,
    task_id: UUID,
    points_cost: int,
) -> None:
    step = await db.get(AgentStep, step_id)
    if step is None:
        raise AppException("批量生产步骤不存在", code=40951, status_code=409)
    task_field = _task_field(phase)
    task_ids = dict((step.extra or {}).get(task_field) or {})
    task_ids[str(scope_id)] = str(task_id)
    attempts = _attempts(step, phase)
    attempts[str(scope_id)] = attempts.get(str(scope_id), 0) + 1
    step.extra = {
        **(step.extra or {}),
        task_field: task_ids,
        _attempt_field(phase): attempts,
        "submitted_points": int((step.extra or {}).get("submitted_points") or 0) + points_cost,
    }
    if points_cost:
        await db.execute(
            update(AgentProduction)
            .where(AgentProduction.id == production_id)
            .values(
                estimated_points=AgentProduction.estimated_points + points_cost,
                consumed_points=AgentProduction.consumed_points + points_cost,
                lock_version=AgentProduction.lock_version + 1,
            )
        )
    await db.commit()


async def _record_dispatch_error(
    db: AsyncSession,
    production_id: UUID,
    step_id: UUID,
    phase: BatchPhase,
    scope_id: UUID,
    message: str,
) -> None:
    step = await db.get(AgentStep, step_id)
    if step is None:
        return
    attempts = _attempts(step, phase)
    attempts[str(scope_id)] = attempts.get(str(scope_id), 0) + 1
    step.extra = {**(step.extra or {}), _attempt_field(phase): attempts}
    if phase == "storyboards":
        item = await db.get(ProjectChapter, scope_id)
        status_key = "storyboard_analysis_status"
    else:
        item = await db.get(ProjectStoryboard, scope_id)
        status_key = f"{'image' if phase == 'images' else 'video'}_generation_status"
    if item is not None:
        item.extra = {
            **(item.extra or {}),
            status_key: "failed",
            f"{status_key}_error": message,
        }
    production = await db.get(AgentProduction, production_id)
    if production is not None:
        production.error_summary = message
    await db.commit()


async def _account_refunded_tasks(db: AsyncSession, context: BatchContext) -> None:
    if context.step is None:
        return
    accounted = set((context.step.extra or {}).get("production_refund_accounted_task_ids") or [])
    task_ids = _all_task_ids(context.step)
    pending_ids = [value for value in task_ids if str(value) not in accounted]
    if not pending_ids:
        return
    result = await db.execute(
        select(UserTaskRecord).where(
            UserTaskRecord.id.in_(pending_ids),
            UserTaskRecord.status == "failed",
        )
    )
    refunded_points = 0
    for task in result.scalars().all():
        if (task.extra or {}).get("refund_transaction_id"):
            accounted.add(str(task.id))
            refunded_points += task.points_cost
    if refunded_points:
        context.production.consumed_points = max(
            0,
            context.production.consumed_points - refunded_points,
        )
        context.step.extra = {
            **(context.step.extra or {}),
            "production_refund_accounted_task_ids": sorted(accounted),
        }


async def _refresh_status(db: AsyncSession, production_id: UUID, user_id: UUID) -> Dict[str, Any]:
    context = await _get_context(db, production_id, user_id, lock=True)
    before = _state_signature(context)
    await _reconcile(db, context)
    if _state_signature(context) != before:
        context.production.lock_version += 1
    await db.commit()
    return await _status(db, context)


async def _status(db: AsyncSession, context: BatchContext) -> Dict[str, Any]:
    phase = _phase(context)
    video_model = await _optional_model(db, context.production, "video_model_id")
    selected_video_model_id = (
        video_model.id
        if video_model is not None
        and video_model.model_type == "video"
        and video_model.is_enabled
        else None
    )
    requires_video_model = phase == "videos" and selected_video_model_id is None
    blocked = (
        set((context.step.extra or {}).get("blocked_storyboard_ids") or [])
        if context.step
        else set()
    )
    episodes = []
    for chapter in context.chapters:
        storyboards = [item for item in context.storyboards if item.chapter_id == chapter.id]
        episodes.append(
            {
                "chapter_id": chapter.id,
                "episode_number": int((chapter.extra or {}).get("episode_number") or 0),
                "title": chapter.title,
                "storyboard_analysis_status": _chapter_status(chapter),
                "storyboard_count": len(storyboards),
                "image_status_counts": _media_counts(storyboards, "image"),
                "video_status_counts": _media_counts(storyboards, "video"),
                "blocked_count": sum(str(item.id) in blocked for item in storyboards),
            }
        )
    available_slots = await _available_task_slots(
        db,
        context.production.user_id,
        phase if phase in {"storyboards", "images", "videos"} else "images",
    )
    if phase in {"storyboards", "images", "videos"}:
        eligible_count = len(_eligible_scope_ids(context, phase))
    elif phase == "not_started" and context.production.current_stage == "batch_production":
        eligible_count = len(context.chapters)
    else:
        eligible_count = 0
    recommended = min(available_slots, eligible_count)
    return {
        "production_id": context.production.id,
        "step_id": context.step.id if context.step else None,
        "core_asset_lock_version": context.core_lock.version,
        "phase": phase,
        "status": context.production.status,
        "current_stage": context.production.current_stage,
        "paused": context.production.status == "paused",
        "remaining_episode_count": len(context.chapters),
        "episodes": episodes,
        "storyboard_count": len(context.storyboards),
        "image_status_counts": _media_counts(context.storyboards, "image"),
        "video_status_counts": _media_counts(context.storyboards, "video"),
        "active_task_count": _total_active_count(context),
        "failed_item_count": _failed_item_count(context),
        "blocked_storyboard_count": len(blocked),
        "quality_issues": list((context.step.extra or {}).get("quality_issues") or [])
        if context.step
        else [],
        "estimated_remaining_points": await _estimated_remaining_points(db, context),
        "submitted_points": int((context.step.extra or {}).get("submitted_points") or 0)
        if context.step
        else 0,
        "recommended_batch_size": min(max(recommended, 0), 50),
        "selected_video_model_id": selected_video_model_id,
        "requires_video_model": requires_video_model,
        "can_dispatch": context.production.status not in {"paused", "completed", "cancelled"}
        and context.production.current_stage != "core_asset_change_review"
        and not requires_video_model
        and (
            phase in {"storyboards", "images", "videos"}
            or (phase == "not_started" and context.production.current_stage == "batch_production")
        ),
        "is_complete": phase == "completed",
    }


async def _estimated_remaining_points(db: AsyncSession, context: BatchContext) -> int:
    text_model = await _optional_model(db, context.production, "text_model_id")
    image_model = await _optional_model(db, context.production, "image_model_id")
    video_model = await _optional_model(db, context.production, "video_model_id")
    text_count = sum(
        _chapter_status(chapter) not in ACTIVE_STATUSES | SUCCESS_STATUSES
        for chapter in context.chapters
    )
    future_shots = sum(
        int((chapter.extra or {}).get("estimated_shot_count") or 1)
        for chapter in context.chapters
        if _chapter_status(chapter) not in SUCCESS_STATUSES
    )
    image_count = future_shots + sum(
        _media_status(storyboard, "image") not in ACTIVE_STATUSES | SUCCESS_STATUSES
        for storyboard in context.storyboards
    )
    video_count = future_shots + sum(
        _media_status(storyboard, "video") not in ACTIVE_STATUSES | SUCCESS_STATUSES
        for storyboard in context.storyboards
    )
    total = 0
    if text_model is not None:
        total += text_count * calculate_submission_points_cost(text_model, "text")
    if image_model is not None:
        total += image_count * calculate_submission_points_cost(image_model, "image")
    if video_model is not None:
        total += video_count * _video_cost(context.production, video_model)
    return total


async def _phase_model(
    db: AsyncSession,
    production: AgentProduction,
    phase: BatchPhase,
) -> AiModel:
    key, model_type = {
        "storyboards": ("text_model_id", "text"),
        "images": ("image_model_id", "image"),
        "videos": ("video_model_id", "video"),
    }[phase]
    model = await _optional_model(db, production, key)
    if model is None or model.model_type != model_type or not model.is_enabled:
        if phase == "videos":
            raise AppException("请先选择视频模型", code=40981, status_code=409)
        raise AppException(
            f"{model_type} 模型不存在、未启用或类型不匹配", code=40404, status_code=404
        )
    return model


async def _optional_model(
    db: AsyncSession,
    production: AgentProduction,
    key: str,
) -> Optional[AiModel]:
    model_id = _optional_uuid((production.production_spec or {}).get(key))
    return await db.get(AiModel, model_id) if model_id is not None else None


async def _available_task_slots(
    db: AsyncSession,
    user_id: UUID,
    phase: str,
) -> int:
    active_since = beijing_datetime() - timedelta(
        hours=max(1, settings.user_pending_task_window_hours)
    )
    total_result = await db.execute(
        select(func.count())
        .select_from(UserTaskRecord)
        .where(
            UserTaskRecord.user_id == user_id,
            UserTaskRecord.status.in_(("pending", "running")),
            UserTaskRecord.updated_at >= active_since,
        )
    )
    total_active = int(total_result.scalar_one() or 0)
    total_slots = (
        max(0, settings.user_pending_task_limit - total_active)
        if settings.user_pending_task_limit > 0
        else 50
    )
    if phase == "storyboards":
        return total_slots
    media_result = await db.execute(
        select(func.count())
        .select_from(UserTaskRecord)
        .where(
            UserTaskRecord.user_id == user_id,
            UserTaskRecord.status.in_(("pending", "running")),
            UserTaskRecord.generation_type.in_(
                ("image", "video", "asset_image_generate", "storyboard_image", "storyboard_video")
            ),
            UserTaskRecord.updated_at >= active_since,
        )
    )
    media_active = int(media_result.scalar_one() or 0)
    media_slots = (
        max(0, settings.user_pending_media_task_limit - media_active)
        if settings.user_pending_media_task_limit > 0
        else 50
    )
    return min(total_slots, media_slots)


async def _ensure_budget(
    db: AsyncSession,
    production: AgentProduction,
    user_id: UUID,
    model: AiModel,
    estimated_cost: int,
) -> None:
    if (
        production.max_points is not None
        and production.consumed_points + estimated_cost > production.max_points
    ):
        raise AppException("整剧任务已达到积分预算上限", code=40052, status_code=400)
    await ensure_user_points_enough(
        db,
        user_id,
        max(estimated_cost, model_minimum_balance_points(model)),
    )


def _scope_submission_cost(
    context: BatchContext,
    model: AiModel,
    phase: BatchPhase,
    _scope_id: UUID,
) -> int:
    if phase == "storyboards":
        return calculate_submission_points_cost(model, "text")
    if phase == "images":
        return calculate_submission_points_cost(model, "image")
    return _video_cost(context.production, model)


def _video_cost(production: AgentProduction, model: AiModel) -> int:
    spec = production.production_spec or {}
    return calculate_submission_points_cost(
        model,
        "video",
        {
            "duration_seconds": int(spec.get("default_shot_duration_seconds") or 5),
            "resolution": str(spec.get("video_resolution") or "720p"),
            "generate_audio": bool(spec.get("generate_audio", False)),
        },
    )


def _failed_item_count(context: BatchContext) -> int:
    if context.step is None:
        return 0
    max_attempts = _max_attempts(context.production)
    count = len(set((context.step.extra or {}).get("blocked_storyboard_ids") or []))
    count += sum(
        _chapter_status(chapter) == "failed"
        and _attempts(context.step, "storyboards").get(str(chapter.id), 0) >= max_attempts
        for chapter in context.chapters
    )
    count += sum(
        _media_status(storyboard, "image") == "failed"
        and _attempts(context.step, "images").get(str(storyboard.id), 0) >= max_attempts
        for storyboard in context.storyboards
    )
    count += sum(
        _media_status(storyboard, "video") == "failed"
        and _attempts(context.step, "videos").get(str(storyboard.id), 0) >= max_attempts
        for storyboard in context.storyboards
    )
    return count


def _total_active_count(context: BatchContext) -> int:
    return sum(_chapter_status(chapter) in ACTIVE_STATUSES for chapter in context.chapters) + sum(
        _media_status(storyboard, kind) in ACTIVE_STATUSES
        for storyboard in context.storyboards
        for kind in ("image", "video")
    )


def _completed_episode_count(context: BatchContext) -> int:
    completed = 0
    for chapter in context.chapters:
        if _chapter_status(chapter) == "skipped":
            completed += 1
            continue
        storyboards = [item for item in context.storyboards if item.chapter_id == chapter.id]
        if (
            _chapter_status(chapter) in SUCCESS_STATUSES
            and storyboards
            and all(_media_status(item, "video") in SUCCESS_STATUSES for item in storyboards)
        ):
            completed += 1
    return completed


def _batch_quality_issues(context: BatchContext) -> List[Dict[str, Any]]:
    active_chapters = [
        chapter for chapter in context.chapters if _chapter_status(chapter) != "skipped"
    ]
    if not active_chapters:
        return []
    active_chapter_ids = {chapter.id for chapter in active_chapters}
    storyboards = [
        storyboard
        for storyboard in context.storyboards
        if storyboard.chapter_id in active_chapter_ids
    ]
    issues = storyboard_quality_issues(
        context.production,
        active_chapters,
        storyboards,
    )
    manually_resolved_ids = {
        str(storyboard.id)
        for storyboard in storyboards
        if (storyboard.extra or {}).get("agent_manual_resolutions")
    }
    return [
        issue
        for issue in issues
        if not issue.get("storyboard_id")
        or str(issue["storyboard_id"]) not in manually_resolved_ids
    ]


def _media_counts(storyboards: List[ProjectStoryboard], kind: str) -> Dict[str, int]:
    counts = {
        "not_started": 0,
        "pending": 0,
        "running": 0,
        "success": 0,
        "selected": 0,
        "failed": 0,
        "invalidated": 0,
    }
    for storyboard in storyboards:
        status = _media_status(storyboard, kind)
        counts[status] = counts.get(status, 0) + 1
    return counts


def _chapter_status(chapter: ProjectChapter) -> str:
    return str((chapter.extra or {}).get("storyboard_analysis_status") or "not_started")


def _media_status(storyboard: ProjectStoryboard, kind: str) -> str:
    return str((storyboard.extra or {}).get(f"{kind}_generation_status") or "not_started")


def _attempts(step: AgentStep, phase: BatchPhase) -> Dict[str, int]:
    return {
        str(key): int(value)
        for key, value in dict((step.extra or {}).get(_attempt_field(phase)) or {}).items()
    }


def _attempt_field(phase: BatchPhase) -> str:
    return f"{'storyboard' if phase == 'storyboards' else phase[:-1]}_attempts"


def _task_field(phase: BatchPhase) -> str:
    return f"{'storyboard' if phase == 'storyboards' else phase[:-1]}_task_ids"


def _all_task_ids(step: AgentStep) -> List[UUID]:
    values = []
    for field in ("storyboard_task_ids", "image_task_ids", "video_task_ids"):
        values.extend(dict((step.extra or {}).get(field) or {}).values())
    return [value for value in (_optional_uuid(item) for item in values) if value is not None]


def _max_attempts(production: AgentProduction) -> int:
    return 1 + max(0, int((production.production_spec or {}).get("retry_limit") or 0))


def _phase(context: BatchContext) -> str:
    if context.step is None:
        return "not_started"
    return str((context.step.extra or {}).get("phase") or "storyboards")


def _set_phase(step: AgentStep, phase: str) -> None:
    step.extra = {**(step.extra or {}), "phase": phase}


def _state_signature(context: BatchContext) -> Tuple[str, str, str, Optional[str]]:
    return (
        _phase(context),
        context.production.status,
        context.production.current_stage,
        context.step.status if context.step else None,
    )


def _append_idempotency_key(step: AgentStep, value: str) -> None:
    keys = list((step.extra or {}).get("dispatch_idempotency_keys") or [])
    if value not in keys:
        keys.append(value)
    step.extra = {**(step.extra or {}), "dispatch_idempotency_keys": keys[-50:]}


def _check_core_lock_version(context: BatchContext, expected: int) -> None:
    if context.core_lock.version != expected:
        raise AppException(
            f"核心资产锁版本冲突，当前版本为 {context.core_lock.version}",
            code=40955,
            status_code=409,
            data={
                "expected_core_asset_lock_version": expected,
                "current_core_asset_lock_version": context.core_lock.version,
            },
        )


def _assert_dispatchable_production(production: AgentProduction) -> None:
    if production.status in {"completed", "cancelled"}:
        raise AppException("当前整剧状态不允许批量生产", code=40951, status_code=409)
    if production.current_stage == "core_asset_change_review":
        raise AppException("核心资产变更尚未重新锁定", code=40950, status_code=409)
    if production.status == "paused":
        raise AppException("整剧任务已暂停，不再创建新任务", code=40952, status_code=409)


def _move_production_status(production: AgentProduction, target: str) -> None:
    if production.status == target:
        return
    if production.status == "partially_failed" and target in {"waiting_approval", "completed"}:
        production.status = transition_production_status(production.status, "running")
    production.status = transition_production_status(production.status, target)


def _move_step_status(step: AgentStep, target: str) -> None:
    if step.status == target:
        return
    if step.status == "failed" and target == "running":
        step.status = transition_step_status(step.status, "queued")
    step.status = transition_step_status(step.status, target)


def _storyboard_image_url(storyboard: ProjectStoryboard) -> str:
    value = str((storyboard.extra or {}).get("image_generation_result") or "").strip()
    if not value:
        raise AppException("故事板图片结果不存在", code=40953, status_code=409)
    return value


def _image_aspect_ratio(value: str) -> str:
    return value if value in {"16:9", "9:16", "1:1", "4:3", "3:4", "3:2", "2:3", "21:9"} else "16:9"


def _assert_job_action_allowed(context: BatchContext) -> None:
    if context.step is None:
        raise AppException("批量生产尚未开始", code=40960, status_code=409)
    if context.production.status in {"paused", "completed", "cancelled"}:
        raise AppException("当前整剧状态不允许处理批量任务", code=40960, status_code=409)
    if context.production.current_stage == "core_asset_change_review":
        raise AppException("核心资产变更尚未重新锁定", code=40950, status_code=409)


def _job_phase(stage: str) -> BatchPhase:
    return {"storyboard": "storyboards", "image": "images", "video": "videos"}[stage]  # type: ignore[return-value]


def _validated_retry_scope_ids(
    context: BatchContext,
    phase: BatchPhase,
    payload: AgentJobRetryRequest,
) -> List[UUID]:
    if context.step is None:
        raise AppException("批量生产尚未开始", code=40960, status_code=409)
    valid_ids = _phase_scope_ids(context, phase)
    if any(scope_id not in valid_ids for scope_id in payload.scope_ids):
        raise AppException("重试目标不属于剩余剧集批量范围", code=40961, status_code=409)
    blocked = set((context.step.extra or {}).get("blocked_storyboard_ids") or [])
    attempts = _attempts(context.step, phase)
    max_attempts = _max_attempts(context.production)
    for scope_id in payload.scope_ids:
        status = _scope_status(context, phase, scope_id)
        if status not in {"failed", "invalidated", "skipped"}:
            raise AppException(
                f"目标 {scope_id} 当前状态 {status} 不允许重试",
                code=40961,
                status_code=409,
            )
        if phase == "images" and str(scope_id) in blocked:
            raise AppException(
                "分镜仍有核心资产绑定错误，请先修复或跳过", code=40962, status_code=409
            )
        if phase == "videos":
            storyboard = next(item for item in context.storyboards if item.id == scope_id)
            if _media_status(storyboard, "image") not in SUCCESS_STATUSES:
                raise AppException("故事板图片尚未就绪，不能重试视频", code=40963, status_code=409)
        if (
            attempts.get(str(scope_id), 0) >= max_attempts
            and not payload.confirm_over_retry_limit
            and not uses_agent_workflow_v2(context.production)
        ):
            raise AppException(
                "目标已达到自动重试上限，需要显式确认后重试",
                code=40964,
                status_code=409,
            )
    return payload.scope_ids


def _validated_skip_scope_ids(
    context: BatchContext,
    phase: BatchPhase,
    scope_ids: List[UUID],
) -> List[UUID]:
    valid_ids = _phase_scope_ids(context, phase)
    if any(scope_id not in valid_ids for scope_id in scope_ids):
        raise AppException("跳过目标不属于剩余剧集批量范围", code=40961, status_code=409)
    for scope_id in scope_ids:
        status = _scope_status(context, phase, scope_id)
        if status in ACTIVE_STATUSES or status == "skipped":
            raise AppException(
                f"目标 {scope_id} 当前状态 {status} 不允许跳过",
                code=40961,
                status_code=409,
            )
        if phase == "storyboards" and any(
            storyboard.chapter_id == scope_id for storyboard in context.storyboards
        ):
            raise AppException("该剧集已有分镜，请在镜头级处理视频", code=40965, status_code=409)
    return scope_ids


def _phase_scope_ids(context: BatchContext, phase: BatchPhase) -> set[UUID]:
    if phase == "storyboards":
        return {chapter.id for chapter in context.chapters}
    return {storyboard.id for storyboard in context.storyboards}


def _scope_status(context: BatchContext, phase: BatchPhase, scope_id: UUID) -> str:
    if phase == "storyboards":
        chapter = next(item for item in context.chapters if item.id == scope_id)
        return _chapter_status(chapter)
    storyboard = next(item for item in context.storyboards if item.id == scope_id)
    return _media_status(storyboard, "image" if phase == "images" else "video")


def _prepare_manual_job_action(
    context: BatchContext,
    phase: BatchPhase,
    idempotency_key: str,
    action: str,
) -> None:
    if context.step is None:
        return
    placeholder = {
        "action": action,
        "stage": phase[:-1] if phase != "storyboards" else "storyboard",
        "requested_count": 0,
        "affected_count": 0,
        "task_record_ids": [],
        "affected_scope_ids": [],
        "points_cost": 0,
        "idempotent": False,
    }
    _store_job_action_result(context.step, idempotency_key, placeholder)
    _set_phase(context.step, phase)
    _move_step_status(context.step, "running")
    context.step.finished_at = None
    _move_production_status(context.production, "running")
    context.production.current_stage = f"batch_{phase}"
    context.production.error_summary = None
    context.production.lock_version += 1


def _job_action_replay(step: Optional[AgentStep], idempotency_key: str) -> Optional[Dict[str, Any]]:
    if step is None:
        return None
    result = dict(((step.extra or {}).get("job_action_results") or {}).get(idempotency_key) or {})
    return result or None


def _store_job_action_result(
    step: AgentStep,
    idempotency_key: str,
    result: Dict[str, Any],
) -> None:
    stored = dict((step.extra or {}).get("job_action_results") or {})
    stored[idempotency_key] = {
        **result,
        "task_record_ids": [str(value) for value in result.get("task_record_ids") or []],
        "affected_scope_ids": [str(value) for value in result.get("affected_scope_ids") or []],
        "idempotent": False,
    }
    while len(stored) > 50:
        stored.pop(next(iter(stored)))
    step.extra = {**(step.extra or {}), "job_action_results": stored}


async def _complete_job_action(
    db: AsyncSession,
    production_id: UUID,
    user_id: UUID,
    idempotency_key: str,
    result: Dict[str, Any],
) -> None:
    context = await _get_context(db, production_id, user_id, lock=True)
    if context.step is None:
        return
    _store_job_action_result(context.step, idempotency_key, result)
    db.add(
        AgentEvent(
            production_id=production_id,
            step_id=context.step.id,
            actor_user_id=user_id,
            event_type="batch.jobs_retried",
            source="user",
            payload={
                "stage": result["stage"],
                "scope_ids": [str(value) for value in result["affected_scope_ids"]],
                "task_record_ids": [str(value) for value in result["task_record_ids"]],
                "points_cost": result["points_cost"],
                "idempotency_key": idempotency_key,
            },
        )
    )
    await db.commit()


async def _resolve_storyboard_manually(
    db: AsyncSession,
    context: BatchContext,
    storyboard: ProjectStoryboard,
    stage: str,
    reason: str,
    replacement_url: Optional[str],
    resolved_at: str,
) -> None:
    _mark_scope_manually_resolved(storyboard, stage, reason, replacement_url, resolved_at)
    if replacement_url:
        await db.execute(
            update(ProjectGeneratedAsset)
            .where(
                ProjectGeneratedAsset.project_id == context.production.project_id,
                ProjectGeneratedAsset.user_id == context.production.user_id,
                ProjectGeneratedAsset.target_type == "storyboard",
                ProjectGeneratedAsset.target_id == storyboard.id,
                ProjectGeneratedAsset.media_type == stage,
                ProjectGeneratedAsset.is_selected.is_(True),
            )
            .values(is_selected=False)
        )
        history = ProjectGeneratedAsset(
            project_id=context.production.project_id,
            chapter_id=storyboard.chapter_id,
            user_id=context.production.user_id,
            target_type="storyboard",
            target_id=storyboard.id,
            media_type=stage,
            result_url=replacement_url,
            result_urls=[replacement_url],
            status="success",
            is_selected=True,
            extra={"manual_replacement": True, "reason": reason},
            is_enabled=True,
        )
        db.add(history)
        await db.flush()
        storyboard.extra = {
            **(storyboard.extra or {}),
            f"{stage}_generation_status": "selected",
            f"{stage}_generation_result": replacement_url,
            f"{stage}_generation_history_id": str(history.id),
        }
        return
    storyboard.extra = {**(storyboard.extra or {}), f"{stage}_generation_status": "skipped"}
    if stage == "image":
        _mark_scope_manually_resolved(storyboard, "video", reason, None, resolved_at)
        storyboard.extra = {**(storyboard.extra or {}), "video_generation_status": "skipped"}


def _mark_scope_manually_resolved(
    item: Any,
    stage: str,
    reason: str,
    replacement_url: Optional[str],
    resolved_at: str,
) -> None:
    resolutions = dict((item.extra or {}).get("agent_manual_resolutions") or {})
    resolutions[stage] = {
        "action": "replacement" if replacement_url else "skip",
        "reason": reason,
        "replacement_url": replacement_url,
        "resolved_at": resolved_at,
    }
    item.extra = {**(item.extra or {}), "agent_manual_resolutions": resolutions}


def _remove_blocked_storyboard(step: AgentStep, storyboard_id: UUID) -> None:
    value = str(storyboard_id)
    blocked = [
        item
        for item in (step.extra or {}).get("blocked_storyboard_ids") or []
        if str(item) != value
    ]
    issues = [
        issue
        for issue in (step.extra or {}).get("quality_issues") or []
        if str(issue.get("storyboard_id") or "") != value
    ]
    step.extra = {
        **(step.extra or {}),
        "blocked_storyboard_ids": blocked,
        "quality_issues": issues,
    }


def _optional_uuid(value: Any) -> Optional[UUID]:
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None
