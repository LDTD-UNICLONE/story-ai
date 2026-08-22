from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException
from app.core.timezone import beijing_datetime
from app.models.agent_core_asset import AgentCoreAssetLock
from app.models.agent_production import AgentCheckpoint, AgentEvent, AgentProduction, AgentStep
from app.models.ai_model import AiModel
from app.models.project import Project
from app.models.project_chapter import ProjectChapter
from app.models.project_storyboard import ProjectStoryboard
from app.models.user import User
from app.schemas.agent_pilot_production import AgentPilotActionRequest
from app.schemas.project_storyboard import (
    ProjectStoryboardAnalyzeRequest,
    ProjectStoryboardImageGenerateRequest,
    ProjectStoryboardVideoGenerateRequest,
)
from app.services.agent_production_state import (
    transition_checkpoint_status,
    transition_production_status,
    transition_step_status,
)
from app.services.agent_storyboard_bindings import (
    bind_storyboards_to_core_lock,
    storyboard_asset_ids,
    storyboard_quality_issues,
)
from app.services.agent_task_context import build_agent_task_context
from app.services.agent_workflow import agent_pilot_episode_count
from app.services.model_points import (
    calculate_submission_points_cost,
    model_minimum_balance_points,
)
from app.services.points import ensure_user_points_enough
from app.services.project_chapter_processing import get_enabled_text_model_or_404
from app.services.project_storyboard_images import submit_storyboard_image_generation
from app.services.project_storyboard_videos import submit_storyboard_video_generation
from app.services.project_storyboards import (
    reconcile_storyboard_media_tasks,
    submit_storyboard_analysis,
)


MediaKind = Literal["image", "video"]
SUCCESS_MEDIA_STATUSES = {"success", "selected"}
ACTIVE_MEDIA_STATUSES = {"pending", "running"}


@dataclass
class PilotContext:
    production: AgentProduction
    project: Project
    core_lock: AgentCoreAssetLock
    step: Optional[AgentStep]
    chapters: List[ProjectChapter]
    storyboards: List[ProjectStoryboard]
    storyboard_checkpoint: Optional[AgentCheckpoint]
    final_checkpoint: Optional[AgentCheckpoint]


async def get_pilot_production(
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
    return _status(context)


async def start_pilot_storyboards(
    db: AsyncSession,
    production_id: UUID,
    user: User,
    payload: AgentPilotActionRequest,
) -> Dict[str, Any]:
    context = await _get_context(db, production_id, user.id, lock=True)
    _check_core_lock_version(context, payload.expected_core_asset_lock_version)
    _assert_production_available(context.production)
    if context.step is not None and _phase(context) not in {"storyboards"}:
        return await _refresh_status(db, production_id, user.id)
    if context.step is None and context.production.current_stage != "pilot_production":
        raise AppException("当前整剧阶段尚未进入试播生产", code=40941, status_code=409)

    eligible = [
        chapter
        for chapter in context.chapters
        if _chapter_analysis_status(chapter) not in {"pending", "running", "success"}
    ]
    text_model_id = _production_model_id(context.production, "text_model_id")
    text_model = await get_enabled_text_model_or_404(db, text_model_id)
    estimated_cost = len(eligible) * calculate_submission_points_cost(text_model, "text")
    await _ensure_budget(db, context.production, user.id, text_model, estimated_cost)

    if context.step is None:
        now = beijing_datetime()
        step = AgentStep(
            production_id=context.production.id,
            stage="pilot_production",
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
                "core_asset_lock_id": str(context.core_lock.id),
                "core_asset_lock_version": context.core_lock.version,
                "chapter_ids": [str(chapter.id) for chapter in context.chapters],
                "storyboard_task_ids": {},
                "image_task_ids": {},
                "video_task_ids": {},
                "submitted_points": 0,
                "storyboard_idempotency_keys": [payload.idempotency_key],
            },
        )
        db.add(step)
        await db.flush()
        checkpoint = AgentCheckpoint(
            production_id=context.production.id,
            step_id=step.id,
            checkpoint_type="pilot_storyboard_review",
            status="pending",
            summary="试播集分镜草案生成中。",
            impact={"chapter_count": len(context.chapters)},
            extra={},
        )
        db.add(checkpoint)
        for chapter in context.chapters:
            chapter.extra = {
                **(chapter.extra or {}),
                "agent_pilot_step_id": str(step.id),
                "agent_pilot_core_asset_lock_id": str(context.core_lock.id),
                "agent_pilot_core_asset_lock_version": context.core_lock.version,
            }
        context.step = step
        context.storyboard_checkpoint = checkpoint
        context.production.current_stage = "pilot_storyboards"
        _move_production_status(context.production, "running")
        context.production.lock_version += 1
        db.add(
            AgentEvent(
                production_id=context.production.id,
                step_id=step.id,
                actor_user_id=user.id,
                event_type="pilot.storyboards_started",
                source="user",
                payload={
                    "chapter_ids": [str(chapter.id) for chapter in context.chapters],
                    "core_asset_lock_version": context.core_lock.version,
                    "idempotency_key": payload.idempotency_key,
                },
            )
        )
        await db.commit()
    else:
        _append_idempotency_key(
            context.step, "storyboard_idempotency_keys", payload.idempotency_key
        )
        _move_production_status(context.production, "running")
        context.production.current_stage = "pilot_storyboards"
        await db.commit()

    for chapter in eligible:
        try:
            task_record, points_cost = await submit_storyboard_analysis(
                db,
                project_id=context.production.project_id,
                chapter_id=chapter.id,
                user=user,
                payload=ProjectStoryboardAnalyzeRequest(ai_model_id=text_model.id),
                agent_context=_agent_task_context(
                    context,
                    stage="pilot_storyboards",
                    scope_type="chapter",
                    scope_id=chapter.id,
                ),
            )
        except AppException as exc:
            chapter.extra = {
                **(chapter.extra or {}),
                "storyboard_analysis_status": "failed",
                "storyboard_analysis_error": exc.message,
            }
            await db.commit()
            continue
        await _record_submission(
            db,
            context.production.id,
            context.step.id,
            "storyboard_task_ids",
            chapter.id,
            task_record.id,
            points_cost if task_record.status != "failed" else 0,
        )

    return await _refresh_status(db, production_id, user.id)


async def submit_pilot_media(
    db: AsyncSession,
    production_id: UUID,
    user: User,
    payload: AgentPilotActionRequest,
    kind: MediaKind,
) -> Dict[str, Any]:
    context = await _get_context(db, production_id, user.id, lock=True)
    _check_core_lock_version(context, payload.expected_core_asset_lock_version)
    _assert_production_available(context.production)
    if context.step is None:
        raise AppException("试播集分镜尚未生成", code=40941, status_code=409)
    await _reconcile(db, context)
    if kind == "image":
        await _refresh_storyboard_quality(db, context)
    phase = _phase(context)
    allowed = {"storyboard_review", "images"} if kind == "image" else {"images_ready", "videos"}
    if phase not in allowed:
        raise AppException("当前试播阶段不允许提交该媒体任务", code=40941, status_code=409)
    if kind == "image" and _error_count(context):
        raise AppException("分镜质检仍有阻断错误，不能生成故事板图片", code=40942, status_code=409)
    if kind == "video" and not _all_media_success(context.storyboards, "image"):
        raise AppException("故事板图片尚未全部生成成功", code=40943, status_code=409)

    eligible = [
        storyboard
        for storyboard in context.storyboards
        if _media_status(storyboard, kind) not in SUCCESS_MEDIA_STATUSES | ACTIVE_MEDIA_STATUSES
    ]
    model_key = "image_model_id" if kind == "image" else "video_model_id"
    model = await _enabled_model_or_404(
        db,
        _production_model_id(context.production, model_key),
        kind,
    )
    estimated_cost = sum(
        _media_submission_cost(context.production, model, kind) for _storyboard in eligible
    )
    await _ensure_budget(db, context.production, user.id, model, estimated_cost)

    if kind == "image":
        if context.storyboard_checkpoint is None:
            raise AppException("试播分镜确认点不存在", code=40941, status_code=409)
        if context.storyboard_checkpoint.status == "pending":
            context.storyboard_checkpoint.status = transition_checkpoint_status(
                context.storyboard_checkpoint.status,
                "approved",
            )
            context.storyboard_checkpoint.approved_by = user.id
            context.storyboard_checkpoint.approved_at = beijing_datetime()
            context.storyboard_checkpoint.extra = {
                **(context.storyboard_checkpoint.extra or {}),
                "confirmation_idempotency_key": payload.idempotency_key,
            }
        _set_phase(context.step, "images")
        _append_idempotency_key(context.step, "image_idempotency_keys", payload.idempotency_key)
        context.production.current_stage = "pilot_images"
    else:
        _set_phase(context.step, "videos")
        _append_idempotency_key(context.step, "video_idempotency_keys", payload.idempotency_key)
        context.production.current_stage = "pilot_videos"
    _move_step_status(context.step, "running")
    _move_production_status(context.production, "running")
    context.production.lock_version += 1
    db.add(
        AgentEvent(
            production_id=context.production.id,
            step_id=context.step.id,
            actor_user_id=user.id,
            event_type=f"pilot.{kind}s_submitted",
            source="user",
            payload={
                "storyboard_count": len(eligible),
                "idempotency_key": payload.idempotency_key,
            },
        )
    )
    await db.commit()

    for storyboard in eligible:
        asset_ids = _storyboard_asset_ids(storyboard)
        try:
            if kind == "image":
                task_record, points_cost = await submit_storyboard_image_generation(
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
                    agent_context=_agent_task_context(
                        context,
                        stage="pilot_images",
                        scope_type="storyboard",
                        scope_id=storyboard.id,
                    ),
                )
            else:
                task_record, points_cost = await submit_storyboard_video_generation(
                    db,
                    project_id=context.production.project_id,
                    chapter_id=storyboard.chapter_id,
                    storyboard_id=storyboard.id,
                    user=user,
                    payload=ProjectStoryboardVideoGenerateRequest(
                        ai_model_id=model.id,
                        generation_mode="reference",
                        resolution=str(
                            (context.production.production_spec or {}).get(
                                "video_resolution",
                                "720p",
                            )
                        ),
                        return_last_frame=True,
                        character_ids=asset_ids["character"],
                        scene_ids=asset_ids["scene"],
                        prop_ids=asset_ids["prop"],
                        uploaded_images=[_storyboard_image_url(storyboard)],
                        extra={
                            "duration_seconds": int(
                                (context.production.production_spec or {}).get(
                                    "default_shot_duration_seconds",
                                    5,
                                )
                            ),
                            "generate_audio": bool(
                                (context.production.production_spec or {}).get(
                                    "generate_audio",
                                    False,
                                )
                            ),
                        },
                        ),
                        agent_context=_agent_task_context(
                            context,
                            stage="pilot_videos",
                            scope_type="storyboard",
                            scope_id=storyboard.id,
                        ),
                    )
        except AppException as exc:
            storyboard.extra = {
                **(storyboard.extra or {}),
                f"{kind}_generation_status": "failed",
                f"{kind}_generation_error": exc.message,
            }
            await db.commit()
            continue
        await _record_submission(
            db,
            context.production.id,
            context.step.id,
            f"{kind}_task_ids",
            storyboard.id,
            task_record.id,
            points_cost if task_record.status != "failed" else 0,
        )

    return await _refresh_status(db, production_id, user.id)


def _agent_task_context(
    context: PilotContext,
    *,
    stage: str,
    scope_type: str,
    scope_id: UUID,
) -> Dict[str, object]:
    if context.step is None:
        raise AppException("试播生产步骤不存在", code=40941, status_code=409)
    return build_agent_task_context(
        production_id=context.production.id,
        step_id=context.step.id,
        stage=stage,
        scope_type=scope_type,
        scope_id=scope_id,
        attempt_number=1,
    )


async def confirm_pilot_production(
    db: AsyncSession,
    production_id: UUID,
    user: User,
    payload: AgentPilotActionRequest,
) -> Dict[str, Any]:
    context = await _get_context(db, production_id, user.id, lock=True)
    _check_core_lock_version(context, payload.expected_core_asset_lock_version)
    _assert_production_available(context.production)
    if context.step is None:
        raise AppException("试播生产尚未开始", code=40941, status_code=409)
    await _reconcile(db, context)
    if _phase(context) == "completed":
        return _status(context)
    if _phase(context) != "final_review" or not _all_media_success(
        context.storyboards,
        "video",
    ):
        raise AppException("试播视频尚未全部生成成功", code=40944, status_code=409)
    checkpoint = context.final_checkpoint
    if checkpoint is None:
        raise AppException("试播最终确认点不存在", code=40941, status_code=409)
    checkpoint.status = transition_checkpoint_status(checkpoint.status, "approved")
    checkpoint.approved_by = user.id
    checkpoint.approved_at = beijing_datetime()
    checkpoint.extra = {
        **(checkpoint.extra or {}),
        "confirmation_idempotency_key": payload.idempotency_key,
    }
    _move_step_status(context.step, "completed")
    context.step.finished_at = beijing_datetime()
    _set_phase(context.step, "completed")
    _move_production_status(context.production, "planning")
    context.production.current_stage = "batch_production"
    context.production.lock_version += 1
    db.add(
        AgentEvent(
            production_id=context.production.id,
            step_id=context.step.id,
            actor_user_id=user.id,
            event_type="pilot.confirmed",
            source="user",
            payload={
                "chapter_ids": [str(chapter.id) for chapter in context.chapters],
                "storyboard_count": len(context.storyboards),
                "idempotency_key": payload.idempotency_key,
            },
        )
    )
    await db.commit()
    return _status(context)


async def _get_context(
    db: AsyncSession,
    production_id: UUID,
    user_id: UUID,
    *,
    lock: bool,
) -> PilotContext:
    production_query = (
        select(AgentProduction, Project)
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
    )
    core_lock = core_lock_result.scalar_one_or_none()
    if core_lock is None:
        raise AppException("核心资产尚未锁定", code=40940, status_code=409)

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
        .limit(pilot_count)
    )
    chapters = list(chapter_result.scalars().all())
    if len(chapters) < pilot_count:
        raise AppException("可用剧集数量少于试播集数量", code=40435, status_code=404)

    step_result = await db.execute(
        select(AgentStep)
        .where(
            AgentStep.production_id == production.id,
            AgentStep.stage == "pilot_production",
            AgentStep.scope_type == "production",
            AgentStep.scope_id == production.id,
            AgentStep.input_version == core_lock.version,
        )
        .order_by(AgentStep.created_at.desc())
        .limit(1)
    )
    step = step_result.scalar_one_or_none()
    checkpoint_by_type: Dict[str, AgentCheckpoint] = {}
    if step is not None:
        checkpoint_result = await db.execute(
            select(AgentCheckpoint).where(AgentCheckpoint.step_id == step.id)
        )
        checkpoint_by_type = {
            checkpoint.checkpoint_type: checkpoint
            for checkpoint in checkpoint_result.scalars().all()
        }
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
    )
    return PilotContext(
        production=production,
        project=project,
        core_lock=core_lock,
        step=step,
        chapters=chapters,
        storyboards=list(storyboard_result.scalars().all()),
        storyboard_checkpoint=checkpoint_by_type.get("pilot_storyboard_review"),
        final_checkpoint=checkpoint_by_type.get("pilot_final_review"),
    )


async def _reconcile(db: AsyncSession, context: PilotContext) -> None:
    step = context.step
    if step is None or context.production.current_stage == "core_asset_change_review":
        return
    phase = _phase(context)
    if phase == "storyboards":
        statuses = [_chapter_analysis_status(chapter) for chapter in context.chapters]
        step.progress_current = sum(status == "success" for status in statuses)
        if any(status == "failed" for status in statuses):
            _move_production_status(context.production, "partially_failed")
        elif statuses and all(status == "success" for status in statuses):
            await _bind_storyboards_to_core_lock(db, context)
            issues = _quality_issues(context)
            step.extra = {**(step.extra or {}), "quality_issues": issues}
            _set_phase(step, "storyboard_review")
            _move_step_status(step, "waiting_approval")
            _move_production_status(context.production, "waiting_approval")
            context.production.current_stage = "pilot_storyboard_review"
            if context.storyboard_checkpoint is not None:
                context.storyboard_checkpoint.summary = (
                    f"试播集已生成 {len(context.storyboards)} 个分镜草案，请审核。"
                )
                context.storyboard_checkpoint.impact = {
                    "chapter_count": len(context.chapters),
                    "storyboard_count": len(context.storyboards),
                    "error_count": sum(item["severity"] == "error" for item in issues),
                    "warning_count": sum(item["severity"] == "warning" for item in issues),
                }
        else:
            _move_production_status(context.production, "running")
        return

    if phase == "storyboard_review":
        await _refresh_storyboard_quality(db, context)
        return

    if phase in {"images_ready", "videos", "final_review"} and not _all_media_success(
        context.storyboards,
        "image",
    ):
        _set_phase(step, "images")
        _move_step_status(step, "running")
        _move_production_status(context.production, "running")
        context.production.current_stage = "pilot_images"
        return
    if phase == "final_review" and not _all_media_success(context.storyboards, "video"):
        _set_phase(step, "videos")
        _move_step_status(step, "running")
        _move_production_status(context.production, "running")
        context.production.current_stage = "pilot_videos"
        return

    if phase not in {"images", "videos"}:
        return
    kind: MediaKind = "image" if phase == "images" else "video"
    for storyboard in context.storyboards:
        await reconcile_storyboard_media_tasks(db, storyboard)
    statuses = [_media_status(storyboard, kind) for storyboard in context.storyboards]
    step.progress_current = sum(status in SUCCESS_MEDIA_STATUSES for status in statuses)
    step.progress_total = len(statuses)
    if any(status == "failed" for status in statuses):
        _move_production_status(context.production, "partially_failed")
        return
    if statuses and all(status in SUCCESS_MEDIA_STATUSES for status in statuses):
        _move_step_status(step, "waiting_approval")
        _move_production_status(context.production, "waiting_approval")
        if kind == "image":
            _set_phase(step, "images_ready")
            context.production.current_stage = "pilot_images_ready"
        else:
            _set_phase(step, "final_review")
            context.production.current_stage = "pilot_review"
            if context.final_checkpoint is None:
                checkpoint = AgentCheckpoint(
                    production_id=context.production.id,
                    step_id=step.id,
                    checkpoint_type="pilot_final_review",
                    status="pending",
                    summary=f"试播集 {len(context.storyboards)} 个镜头视频已就绪，请确认质量。",
                    impact={
                        "chapter_count": len(context.chapters),
                        "storyboard_count": len(context.storyboards),
                        "submitted_points": int((step.extra or {}).get("submitted_points") or 0),
                    },
                    extra={},
                )
                db.add(checkpoint)
                await db.flush()
                context.final_checkpoint = checkpoint
        return
    _move_production_status(context.production, "running")


async def _refresh_storyboard_quality(db: AsyncSession, context: PilotContext) -> None:
    if context.step is None:
        return
    await _bind_storyboards_to_core_lock(db, context)
    issues = _quality_issues(context)
    context.step.extra = {**(context.step.extra or {}), "quality_issues": issues}
    if context.storyboard_checkpoint is not None:
        context.storyboard_checkpoint.impact = {
            **(context.storyboard_checkpoint.impact or {}),
            "storyboard_count": len(context.storyboards),
            "error_count": sum(item["severity"] == "error" for item in issues),
            "warning_count": sum(item["severity"] == "warning" for item in issues),
        }


async def _bind_storyboards_to_core_lock(db: AsyncSession, context: PilotContext) -> None:
    await bind_storyboards_to_core_lock(
        db,
        context.production,
        context.core_lock,
        context.storyboards,
    )


def _quality_issues(context: PilotContext) -> List[Dict[str, Any]]:
    return storyboard_quality_issues(
        context.production,
        context.chapters,
        context.storyboards,
    )


def _status(context: PilotContext) -> Dict[str, Any]:
    phase = _phase(context)
    issues = list((context.step.extra or {}).get("quality_issues") or []) if context.step else []
    episodes = []
    for chapter in context.chapters:
        storyboards = [item for item in context.storyboards if item.chapter_id == chapter.id]
        episodes.append(
            {
                "chapter_id": chapter.id,
                "episode_number": int((chapter.extra or {}).get("episode_number") or 0),
                "title": chapter.title,
                "storyboard_analysis_status": _chapter_analysis_status(chapter),
                "storyboard_count": len(storyboards),
                "image_status_counts": _media_counts(storyboards, "image"),
                "video_status_counts": _media_counts(storyboards, "video"),
            }
        )
    image_counts = _media_counts(context.storyboards, "image")
    video_counts = _media_counts(context.storyboards, "video")
    error_count = sum(item.get("severity") == "error" for item in issues)
    warning_count = sum(item.get("severity") == "warning" for item in issues)
    return {
        "production_id": context.production.id,
        "step_id": context.step.id if context.step else None,
        "core_asset_lock_version": context.core_lock.version,
        "pilot_episode_count": len(context.chapters),
        "phase": phase,
        "status": context.production.status,
        "current_stage": context.production.current_stage,
        "storyboard_checkpoint_id": (
            context.storyboard_checkpoint.id if context.storyboard_checkpoint else None
        ),
        "final_checkpoint_id": context.final_checkpoint.id if context.final_checkpoint else None,
        "episodes": episodes,
        "storyboard_count": len(context.storyboards),
        "image_status_counts": image_counts,
        "video_status_counts": video_counts,
        "issues": issues,
        "error_count": error_count,
        "warning_count": warning_count,
        "submitted_points": int((context.step.extra or {}).get("submitted_points") or 0)
        if context.step
        else 0,
        "can_submit_images": phase in {"storyboard_review", "images"} and error_count == 0,
        "can_submit_videos": phase in {"images_ready", "videos"}
        and _all_media_success(context.storyboards, "image"),
        "can_confirm": phase == "final_review" and _all_media_success(context.storyboards, "video"),
    }


async def _refresh_status(db: AsyncSession, production_id: UUID, user_id: UUID) -> Dict[str, Any]:
    context = await _get_context(db, production_id, user_id, lock=True)
    before = _state_signature(context)
    await _reconcile(db, context)
    if _state_signature(context) != before:
        context.production.lock_version += 1
    await db.commit()
    return _status(context)


async def _record_submission(
    db: AsyncSession,
    production_id: UUID,
    step_id: UUID,
    field: str,
    scope_id: UUID,
    task_id: UUID,
    points_cost: int,
) -> None:
    step = await db.get(AgentStep, step_id)
    if step is None:
        raise AppException("试播生产步骤不存在", code=40941, status_code=409)
    task_ids = dict((step.extra or {}).get(field) or {})
    task_ids[str(scope_id)] = str(task_id)
    step.extra = {
        **(step.extra or {}),
        field: task_ids,
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


async def _enabled_model_or_404(
    db: AsyncSession,
    model_id: UUID,
    model_type: str,
) -> AiModel:
    result = await db.execute(
        select(AiModel).where(
            AiModel.id == model_id,
            AiModel.model_type == model_type,
            AiModel.is_enabled.is_(True),
        )
    )
    model = result.scalar_one_or_none()
    if model is None:
        raise AppException(
            f"{model_type} 模型不存在、未启用或类型不匹配", code=40404, status_code=404
        )
    return model


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


def _media_submission_cost(production: AgentProduction, model: AiModel, kind: MediaKind) -> int:
    if kind == "image":
        return calculate_submission_points_cost(model, "image")
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


def _check_core_lock_version(context: PilotContext, expected: int) -> None:
    if context.core_lock.version != expected:
        raise AppException(
            f"核心资产锁版本冲突，当前版本为 {context.core_lock.version}",
            code=40945,
            status_code=409,
            data={
                "expected_core_asset_lock_version": expected,
                "current_core_asset_lock_version": context.core_lock.version,
            },
        )


def _assert_production_available(production: AgentProduction) -> None:
    if production.status in {"completed", "cancelled", "paused"}:
        raise AppException("当前整剧状态不允许执行试播生产", code=40941, status_code=409)
    if production.current_stage == "core_asset_change_review":
        raise AppException("核心资产变更尚未重新锁定", code=40940, status_code=409)


def _move_production_status(production: AgentProduction, target: str) -> None:
    if production.status == target:
        return
    if production.status == "partially_failed" and target == "waiting_approval":
        production.status = transition_production_status(production.status, "running")
    elif production.status == "waiting_approval" and target == "partially_failed":
        production.status = transition_production_status(production.status, "running")
    production.status = transition_production_status(production.status, target)


def _move_step_status(step: AgentStep, target: str) -> None:
    if step.status == target:
        return
    if step.status == "failed" and target == "running":
        step.status = transition_step_status(step.status, "queued")
    step.status = transition_step_status(step.status, target)


def _set_phase(step: AgentStep, phase: str) -> None:
    step.extra = {**(step.extra or {}), "phase": phase}


def _phase(context: PilotContext) -> str:
    if context.step is None:
        return "not_started"
    return str((context.step.extra or {}).get("phase") or "storyboards")


def _state_signature(context: PilotContext) -> tuple[str, str, str, Optional[str]]:
    return (
        _phase(context),
        context.production.status,
        context.production.current_stage,
        context.step.status if context.step else None,
    )


def _append_idempotency_key(step: AgentStep, field: str, value: str) -> None:
    keys = list((step.extra or {}).get(field) or [])
    if value not in keys:
        keys.append(value)
    step.extra = {**(step.extra or {}), field: keys[-20:]}


def _production_model_id(production: AgentProduction, key: str) -> UUID:
    value = _optional_uuid((production.production_spec or {}).get(key))
    if value is None:
        raise AppException("整剧生产模型配置不完整", code=40941, status_code=409)
    return value


def _chapter_analysis_status(chapter: ProjectChapter) -> str:
    return str((chapter.extra or {}).get("storyboard_analysis_status") or "not_started")


def _media_status(storyboard: ProjectStoryboard, kind: MediaKind) -> str:
    return str((storyboard.extra or {}).get(f"{kind}_generation_status") or "not_started")


def _media_counts(storyboards: List[ProjectStoryboard], kind: MediaKind) -> Dict[str, int]:
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


def _all_media_success(storyboards: List[ProjectStoryboard], kind: MediaKind) -> bool:
    return bool(storyboards) and all(
        _media_status(storyboard, kind) in SUCCESS_MEDIA_STATUSES for storyboard in storyboards
    )


def _storyboard_asset_ids(storyboard: ProjectStoryboard) -> Dict[str, List[UUID]]:
    return storyboard_asset_ids(storyboard)


def _storyboard_image_url(storyboard: ProjectStoryboard) -> str:
    value = str((storyboard.extra or {}).get("image_generation_result") or "").strip()
    if not value:
        raise AppException("故事板图片结果不存在", code=40943, status_code=409)
    return value


def _image_aspect_ratio(value: str) -> str:
    return value if value in {"16:9", "9:16", "1:1", "4:3", "3:4", "3:2", "2:3", "21:9"} else "16:9"


def _error_count(context: PilotContext) -> int:
    return sum(
        item.get("severity") == "error"
        for item in ((context.step.extra or {}).get("quality_issues") or [])
    )


def _optional_uuid(value: Any) -> Optional[UUID]:
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None
