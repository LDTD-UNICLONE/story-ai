import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException
from app.core.public_messages import sanitize_public_message
from app.core.timezone import beijing_datetime
from app.models.agent_core_asset import AgentCoreAssetLock
from app.models.agent_production import AgentEvent, AgentProduction
from app.models.agent_review import AgentEpisodeReview
from app.models.agent_storyboard_media import AgentStoryboardMediaRequest
from app.models.ai_model import AiModel
from app.models.project import Project
from app.models.project_chapter import ProjectChapter
from app.models.project_generated_asset import ProjectGeneratedAsset
from app.models.project_storyboard import ProjectStoryboard
from app.models.task_record import UserTaskRecord
from app.models.user import User
from app.schemas.agent_storyboard_media import (
    AgentEpisodeVideoGenerationRequest,
    AgentStoryboardPrimaryVideoRequest,
    AgentStoryboardVideoGenerationRequest,
)
from app.schemas.project_storyboard import ProjectStoryboardVideoGenerateRequest
from app.services.agent_storyboard_bindings import (
    storyboard_asset_ids,
    storyboard_estimated_duration_seconds,
    storyboard_quality_issues,
)
from app.services.agent_storyboard_episode_state import effective_storyboard_episode_status
from app.services.agent_storyboard_video_inputs import build_agent_storyboard_video_input
from app.services.project_storyboard_videos import (
    storyboard_video_image_limit,
    submit_storyboard_video_generation,
)
from app.services.provider_polling import provider_next_poll_seconds


ACTIVE_STATUSES = {"pending", "running"}
SUCCESS_STATUSES = {"success", "selected"}
EPISODE_BATCH_REUSED_STATUSES = SUCCESS_STATUSES | {"selection_required"}
VIDEO_STATUS_KEYS = (
    "not_started",
    "pending",
    "running",
    "success",
    "selected",
    "failed",
    "invalidated",
    "selection_required",
)


@dataclass
class EpisodeVideoContext:
    production: AgentProduction
    project: Project
    core_lock: AgentCoreAssetLock
    chapter: ProjectChapter
    storyboards: List[ProjectStoryboard]


async def get_agent_episode_videos(
    db: AsyncSession,
    production_id: UUID,
    chapter_id: UUID,
    user_id: UUID,
) -> Dict[str, Any]:
    context = await _get_context(db, production_id, chapter_id, user_id, lock=False)
    _require_episode_ready(context)
    tasks = await _current_video_tasks(db, context.storyboards)
    counts = _status_counts(context.storyboards)
    active_tasks = [task for task in tasks.values() if task.status in ACTIVE_STATUSES]
    if context.storyboards and all(
        _video_status(storyboard) in SUCCESS_STATUSES
        for storyboard in context.storyboards
    ):
        status = "completed"
    elif active_tasks:
        status = "processing"
    elif counts["selection_required"]:
        status = "selection_required"
    elif counts["failed"]:
        status = "failed"
    else:
        status = "ready"
    poll_intervals = [
        provider_next_poll_seconds(task.generation_type, task.status, task.extra or {})
        for task in active_tasks
    ]
    return {
        "production_id": context.production.id,
        "chapter_id": context.chapter.id,
        "episode_number": int((context.chapter.extra or {}).get("episode_number") or 0),
        "title": context.chapter.title,
        "episode_revision": _episode_revision(context.chapter),
        "core_asset_lock_version": context.core_lock.version,
        "status": status,
        "storyboard_count": len(context.storyboards),
        "video_status_counts": counts,
        "active_task_count": len(active_tasks),
        "failed_item_count": counts["failed"],
        "selection_required_count": counts["selection_required"],
        "can_generate": any(
            _video_status(storyboard)
            not in ACTIVE_STATUSES | EPISODE_BATCH_REUSED_STATUSES
            for storyboard in context.storyboards
        ),
        "should_poll": bool(active_tasks),
        "next_poll_seconds": min(
            (value for value in poll_intervals if value is not None),
            default=None,
        ),
        "storyboards": [_video_item(storyboard, tasks) for storyboard in context.storyboards],
    }


async def get_agent_storyboard_video_versions(
    db: AsyncSession,
    production_id: UUID,
    chapter_id: UUID,
    storyboard_id: UUID,
    user_id: UUID,
) -> Dict[str, Any]:
    context = await _get_context(db, production_id, chapter_id, user_id, lock=False)
    _require_episode_ready(context)
    storyboard = _target_storyboards(context, storyboard_id)[0]
    histories = list(
        (
            await db.execute(
                select(ProjectGeneratedAsset)
                .where(
                    ProjectGeneratedAsset.project_id == context.production.project_id,
                    ProjectGeneratedAsset.user_id == user_id,
                    ProjectGeneratedAsset.target_type == "storyboard",
                    ProjectGeneratedAsset.target_id == storyboard.id,
                    ProjectGeneratedAsset.media_type == "video",
                    ProjectGeneratedAsset.is_enabled.is_(True),
                )
                .order_by(
                    ProjectGeneratedAsset.created_at.desc(),
                    ProjectGeneratedAsset.id.desc(),
                )
            )
        )
        .scalars()
        .all()
    )
    selected = next((item for item in histories if item.is_selected), None)
    extra = storyboard.extra or {}
    return {
        "production_id": context.production.id,
        "chapter_id": context.chapter.id,
        "storyboard_id": storyboard.id,
        "selection_revision": int(extra.get("video_selection_revision") or 0),
        "selection_required": bool(extra.get("video_selection_required")),
        "selected_history_id": selected.id if selected is not None else None,
        "latest_history_id": _optional_uuid(extra.get("video_latest_history_id")),
        "items": [_video_version_item(item) for item in histories],
        "total": len(histories),
    }


async def select_agent_storyboard_primary_video(
    db: AsyncSession,
    production_id: UUID,
    chapter_id: UUID,
    storyboard_id: UUID,
    user: User,
    payload: AgentStoryboardPrimaryVideoRequest,
) -> Dict[str, Any]:
    context = await _get_context(db, production_id, chapter_id, user.id, lock=True)
    _require_episode_ready(context)
    storyboard = _target_storyboards(context, storyboard_id)[0]
    storyboard = (
        await db.execute(
            select(ProjectStoryboard)
            .where(ProjectStoryboard.id == storyboard.id)
            .with_for_update()
        )
    ).scalar_one()
    extra = dict(storyboard.extra or {})
    current_revision = int(extra.get("video_selection_revision") or 0)
    if payload.expected_selection_revision != current_revision:
        raise AppException(
            f"主视频选择版本冲突，当前版本为 {current_revision}",
            code=40993,
            status_code=409,
            data={
                "expected_selection_revision": payload.expected_selection_revision,
                "current_selection_revision": current_revision,
            },
        )
    history = (
        await db.execute(
            select(ProjectGeneratedAsset)
            .where(
                ProjectGeneratedAsset.id == payload.history_id,
                ProjectGeneratedAsset.project_id == context.production.project_id,
                ProjectGeneratedAsset.user_id == user.id,
                ProjectGeneratedAsset.target_type == "storyboard",
                ProjectGeneratedAsset.target_id == storyboard.id,
                ProjectGeneratedAsset.media_type == "video",
                ProjectGeneratedAsset.status == "success",
                ProjectGeneratedAsset.is_enabled.is_(True),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if history is None:
        raise AppException("分镜视频候选不存在", code=40445, status_code=404)
    selected_url = payload.result_url or history.result_url
    if selected_url is None and history.result_urls:
        selected_url = history.result_urls[0]
    if selected_url is None:
        raise AppException("视频候选没有可选择的结果地址", code=40036, status_code=400)
    if payload.result_url and payload.result_url not in (history.result_urls or []):
        raise AppException("选择的视频地址不属于该候选", code=40036, status_code=400)
    await db.execute(
        update(ProjectGeneratedAsset)
        .where(
            ProjectGeneratedAsset.project_id == context.production.project_id,
            ProjectGeneratedAsset.user_id == user.id,
            ProjectGeneratedAsset.target_type == "storyboard",
            ProjectGeneratedAsset.target_id == storyboard.id,
            ProjectGeneratedAsset.media_type == "video",
            ProjectGeneratedAsset.is_selected.is_(True),
        )
        .values(is_selected=False)
    )
    history.result_url = selected_url
    history.is_selected = True
    history.updated_at = beijing_datetime()
    next_revision = current_revision + 1
    extra.update(
        {
            "video_generation_status": "selected",
            "video_generation_history_id": str(history.id),
            "video_generation_task_record_id": (
                str(history.task_record_id) if history.task_record_id else ""
            ),
            "video_generation_result": selected_url or "",
            "video_selection_required": False,
            "video_selection_revision": next_revision,
        }
    )
    if history.last_frame_url:
        extra["video_generation_last_frame_url"] = history.last_frame_url
    storyboard.extra = extra
    storyboard.updated_at = beijing_datetime()
    await db.execute(
        update(AgentEpisodeReview)
        .where(
            AgentEpisodeReview.production_id == context.production.id,
            AgentEpisodeReview.chapter_id == context.chapter.id,
            AgentEpisodeReview.status == "approved",
        )
        .values(status="invalidated")
    )
    db.add(
        AgentEvent(
            production_id=context.production.id,
            actor_user_id=user.id,
            event_type="storyboard.primary_video_selected",
            source="user",
            payload={
                "chapter_id": str(context.chapter.id),
                "storyboard_id": str(storyboard.id),
                "history_id": str(history.id),
                "selection_revision": next_revision,
            },
        )
    )
    await db.commit()
    return {
        "storyboard_id": storyboard.id,
        "chapter_id": context.chapter.id,
        "history_id": history.id,
        "selected_url": selected_url,
        "selection_revision": next_revision,
        "selection_required": False,
    }


async def submit_agent_episode_videos(
    db: AsyncSession,
    production_id: UUID,
    chapter_id: UUID,
    user: User,
    payload: AgentEpisodeVideoGenerationRequest,
) -> Dict[str, Any]:
    context = await _get_context(db, production_id, chapter_id, user.id, lock=True)
    return await _submit_agent_videos(
        db,
        context,
        user,
        payload,
        signature=_request_signature(context, payload),
    )


async def submit_agent_storyboard_video(
    db: AsyncSession,
    production_id: UUID,
    chapter_id: UUID,
    storyboard_id: UUID,
    user: User,
    payload: AgentStoryboardVideoGenerationRequest,
) -> Dict[str, Any]:
    context = await _get_context(db, production_id, chapter_id, user.id, lock=True)
    return await _submit_agent_videos(
        db,
        context,
        user,
        payload,
        signature=_storyboard_request_signature(context, storyboard_id, payload),
        target_storyboard_id=storyboard_id,
        expected_storyboard_revision=payload.expected_storyboard_revision,
        custom_prompt=payload.prompt,
        regenerate_success=True,
    )


async def _submit_agent_videos(
    db: AsyncSession,
    context: EpisodeVideoContext,
    user: User,
    payload: AgentEpisodeVideoGenerationRequest,
    *,
    signature: str,
    target_storyboard_id: Optional[UUID] = None,
    expected_storyboard_revision: Optional[int] = None,
    custom_prompt: Optional[str] = None,
    regenerate_success: bool = False,
) -> Dict[str, Any]:
    request = await _existing_request(
        db,
        context.production.id,
        payload.idempotency_key,
    )
    if request is not None:
        _check_request_replay(request, signature, context.chapter.id)
        return await _generation_result(db, request, idempotent_replay=True)

    _check_core_lock_version(context, payload.expected_core_asset_lock_version)
    _require_episode_ready(context)
    _check_episode_revision(context.chapter, payload.expected_episode_revision)
    storyboards = _target_storyboards(context, target_storyboard_id)
    if target_storyboard_id is not None:
        _check_storyboard_revision(storyboards[0], expected_storyboard_revision)
    _validate_storyboards(context, storyboards)
    default_model = await _video_model(db, payload.video_model_id)
    model_cache = {default_model.id: default_model}
    revisions = {storyboard.id: _storyboard_revision(storyboard) for storyboard in storyboards}
    request = AgentStoryboardMediaRequest(
        production_id=context.production.id,
        chapter_id=context.chapter.id,
        user_id=user.id,
        media_type="video",
        idempotency_key=payload.idempotency_key,
        status="pending",
        items=[],
        submitted_points=0,
        extra={
            "request_signature": signature,
            "core_asset_lock_version": context.core_lock.version,
            "episode_revision": payload.expected_episode_revision,
            "generation_scope": "storyboard" if target_storyboard_id else "episode",
            "storyboard_ids": [str(item.id) for item in storyboards],
            "storyboard_revisions": {
                str(storyboard_id): revision
                for storyboard_id, revision in revisions.items()
            },
            "video_model_id": str(default_model.id),
            "video_resolution": payload.video_resolution,
            "duration_seconds": getattr(payload, "duration_seconds", None),
            "video_config_version": getattr(
                payload, "expected_video_config_version", None
            ),
            "custom_prompt": custom_prompt,
        },
    )
    db.add(request)
    await db.commit()
    await db.refresh(request)

    try:
        for storyboard in storyboards:
            current = await db.get(ProjectStoryboard, storyboard.id)
            if current is None or not current.is_enabled:
                request.items = [
                    *(request.items or []),
                    _error_item(storyboard.id, 40410, "分镜组不存在"),
                ]
                await db.commit()
                continue
            expected_revision = revisions[current.id]
            if _storyboard_revision(current) != expected_revision:
                request.items = [
                    *(request.items or []),
                    _error_item(current.id, 40988, "分镜组已修改，请刷新本集后重试"),
                ]
                await db.commit()
                continue
            current_status = _video_status(current)
            if current_status in ACTIVE_STATUSES or (
                not regenerate_success
                and current_status in EPISODE_BATCH_REUSED_STATUSES
            ):
                request.items = [
                    *(request.items or []),
                    {
                        "storyboard_id": str(current.id),
                        "submitted": False,
                        "reused_active_task": current_status in ACTIVE_STATUSES,
                        "task_record_id": str(
                            (current.extra or {}).get("video_generation_task_record_id")
                            or ""
                        ),
                        "status": current_status,
                        "points_cost": 0,
                    },
                ]
                await db.commit()
                continue
            try:
                (
                    model,
                    resolution,
                    duration_seconds,
                    video_config_version,
                ) = await _storyboard_video_parameters(
                    db,
                    current,
                    default_model,
                    payload.video_resolution,
                    use_saved_config=target_storyboard_id is None,
                    model_cache=model_cache,
                    requested_duration_seconds=getattr(
                        payload, "duration_seconds", None
                    ),
                    expected_config_version=getattr(
                        payload, "expected_video_config_version", None
                    ),
                )
                task_record, points_cost = await _submit_storyboard_video(
                    db,
                    context,
                    current,
                    user,
                    payload,
                    request,
                    model,
                    expected_revision,
                    resolution=resolution,
                    duration_seconds=duration_seconds,
                    video_config_version=video_config_version,
                    custom_prompt=custom_prompt,
                )
            except AppException as exc:
                request.items = [
                    *(request.items or []),
                    _error_item(current.id, exc.code, exc.message),
                ]
                await db.commit()
                continue
            submitted = task_record.status != "failed"
            request.items = [
                *(request.items or []),
                {
                    "storyboard_id": str(current.id),
                    "submitted": submitted,
                    "reused_active_task": False,
                    "task_record_id": str(task_record.id),
                    "status": task_record.status,
                    "points_cost": points_cost if submitted else 0,
                    **(
                        {}
                        if submitted
                        else {
                            "error_code": 50301,
                            "error_message": task_record.result or "任务入队失败",
                        }
                    ),
                },
            ]
            if submitted:
                request.submitted_points += points_cost
                await db.execute(
                    update(AgentProduction)
                    .where(AgentProduction.id == context.production.id)
                    .values(
                        estimated_points=AgentProduction.estimated_points + points_cost,
                        consumed_points=AgentProduction.consumed_points + points_cost,
                        lock_version=AgentProduction.lock_version + 1,
                    )
                )
            await db.commit()
        failed_count = sum(bool(item.get("error_message")) for item in request.items or [])
        request.status = "failed" if failed_count == len(request.items or []) else "submitted"
        request.error_summary = "目标视频任务提交失败" if request.status == "failed" else None
        db.add(
            AgentEvent(
                production_id=context.production.id,
                actor_user_id=user.id,
                event_type=(
                    "storyboard.video_generation_requested"
                    if target_storyboard_id
                    else "episode.video_generation_requested"
                ),
                source="user",
                payload={
                    "request_id": str(request.id),
                    "chapter_id": str(context.chapter.id),
                    "episode_revision": payload.expected_episode_revision,
                    "generation_scope": "storyboard" if target_storyboard_id else "episode",
                    "storyboard_ids": [str(item.id) for item in storyboards],
                    "video_model_id": str(default_model.id),
                    "video_resolution": payload.video_resolution,
                    "duration_seconds": getattr(payload, "duration_seconds", None),
                    "video_config_version": getattr(
                        payload, "expected_video_config_version", None
                    ),
                    "custom_prompt": custom_prompt,
                    "submitted_points": request.submitted_points,
                    "idempotency_key": request.idempotency_key,
                },
            )
        )
        await db.commit()
    except Exception as exc:
        request_id = request.id
        await db.rollback()
        request = await db.get(AgentStoryboardMediaRequest, request_id)
        if request is None:
            raise
        request.status = "failed"
        request.error_summary = sanitize_public_message(str(exc) or "目标视频任务提交失败")
        await db.commit()
        raise
    return await _generation_result(db, request, idempotent_replay=False)


async def _submit_storyboard_video(
    db: AsyncSession,
    context: EpisodeVideoContext,
    storyboard: ProjectStoryboard,
    user: User,
    payload: AgentEpisodeVideoGenerationRequest,
    request: AgentStoryboardMediaRequest,
    model: AiModel,
    revision: int,
    *,
    resolution: str,
    duration_seconds: int,
    video_config_version: int,
    custom_prompt: Optional[str] = None,
):
    asset_ids = storyboard_asset_ids(storyboard)
    video_input = await build_agent_storyboard_video_input(
        db,
        context.production,
        storyboard,
        max_images=storyboard_video_image_limit(model),
        prompt_notes=custom_prompt,
    )
    return await submit_storyboard_video_generation(
        db,
        context.production.project_id,
        storyboard.chapter_id,
        storyboard.id,
        user,
        ProjectStoryboardVideoGenerateRequest(
            ai_model_id=model.id,
            generation_mode="reference",
            resolution=resolution,
            return_last_frame=True,
            prompt=custom_prompt,
            character_ids=asset_ids["character"],
            scene_ids=asset_ids["scene"],
            prop_ids=asset_ids["prop"],
            extra={
                "duration_seconds": duration_seconds,
                "generate_audio": False,
            },
        ),
        agent_context={
            "agent_production_id": str(context.production.id),
            "agent_stage": "episode_videos",
            "agent_scope_type": "storyboard",
            "agent_scope_id": str(storyboard.id),
            "agent_storyboard_media_request_id": str(request.id),
            "agent_media_idempotency_key": request.idempotency_key,
            "agent_storyboard_revision": revision,
            "agent_episode_revision": payload.expected_episode_revision,
            "agent_core_asset_lock_version": context.core_lock.version,
            "agent_video_config_version": video_config_version,
            "agent_asset_bindings_snapshot": list(
                (storyboard.extra or {}).get("agent_asset_bindings") or []
            ),
            "agent_custom_prompt": custom_prompt,
            "agent_compiled_prompt": video_input.prompt,
            "agent_reference_images": video_input.reference_images,
            "agent_reference_manifest": video_input.reference_manifest,
            "agent_provider_parameters": {
                "model_id": str(model.id),
                "resolution": resolution,
                "ratio": context.project.generation_ratio,
                "duration_seconds": duration_seconds,
                "generate_audio": False,
                "return_last_frame": True,
            },
        },
    )


async def _get_context(
    db: AsyncSession,
    production_id: UUID,
    chapter_id: UUID,
    user_id: UUID,
    *,
    lock: bool,
) -> EpisodeVideoContext:
    query = (
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
        query = query.with_for_update(of=AgentProduction)
    row = (await db.execute(query)).one_or_none()
    if row is None:
        raise AppException("Agent 项目不存在", code=40430, status_code=404)
    production, project = row
    core_lock = (
        await db.execute(
            select(AgentCoreAssetLock)
            .where(
                AgentCoreAssetLock.production_id == production.id,
                AgentCoreAssetLock.status == "active",
            )
            .order_by(AgentCoreAssetLock.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if core_lock is None:
        raise AppException("核心资产尚未确认", code=40950, status_code=409)
    chapter = (
        await db.execute(
            select(ProjectChapter).where(
                ProjectChapter.id == chapter_id,
                ProjectChapter.project_id == production.project_id,
                ProjectChapter.user_id == user_id,
                ProjectChapter.is_enabled.is_(True),
                ProjectChapter.extra["agent_production_id"].as_string()
                == str(production.id),
            )
        )
    ).scalar_one_or_none()
    if chapter is None:
        raise AppException("Agent 分集不存在", code=40431, status_code=404)
    storyboards = list(
        (
            await db.execute(
                select(ProjectStoryboard)
                .where(
                    ProjectStoryboard.project_id == production.project_id,
                    ProjectStoryboard.chapter_id == chapter.id,
                    ProjectStoryboard.user_id == user_id,
                    ProjectStoryboard.is_enabled.is_(True),
                )
                .order_by(
                    ProjectStoryboard.shot_number,
                    ProjectStoryboard.created_at,
                    ProjectStoryboard.id,
                )
            )
        )
        .scalars()
        .all()
    )
    return EpisodeVideoContext(production, project, core_lock, chapter, storyboards)


def _require_episode_ready(context: EpisodeVideoContext) -> None:
    status = effective_storyboard_episode_status(context.chapter, len(context.storyboards))
    if status != "ready":
        raise AppException(
            "该集分镜尚未分析完成，不能生成视频",
            code=40968,
            status_code=409,
            data={"chapter_id": str(context.chapter.id), "status": status},
        )


def _check_core_lock_version(context: EpisodeVideoContext, expected: int) -> None:
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


def _check_episode_revision(chapter: ProjectChapter, expected: int) -> None:
    current = _episode_revision(chapter)
    if current != expected:
        raise AppException(
            f"分集分镜版本冲突，当前版本为 {current}",
            code=40960,
            status_code=409,
            data={
                "expected_episode_revision": expected,
                "current_episode_revision": current,
            },
        )


def _target_storyboards(
    context: EpisodeVideoContext,
    storyboard_id: Optional[UUID],
) -> List[ProjectStoryboard]:
    if storyboard_id is None:
        return context.storyboards
    storyboard = next(
        (item for item in context.storyboards if item.id == storyboard_id),
        None,
    )
    if storyboard is None:
        raise AppException("Agent 分镜不存在", code=40410, status_code=404)
    return [storyboard]


def _check_storyboard_revision(
    storyboard: ProjectStoryboard,
    expected: Optional[int],
) -> None:
    current = _storyboard_revision(storyboard)
    if expected != current:
        raise AppException(
            f"分镜组版本冲突，当前版本为 {current}",
            code=40959,
            status_code=409,
            data={
                "expected_storyboard_revision": expected,
                "current_storyboard_revision": current,
            },
        )


def _validate_storyboards(
    context: EpisodeVideoContext,
    storyboards: List[ProjectStoryboard],
) -> None:
    issues = storyboard_quality_issues(
        context.production,
        [context.chapter],
        storyboards,
    )
    errors = [item for item in issues if item.get("severity") == "error"]
    if errors:
        raise AppException(
            "本集存在尚未准备完成的分镜组",
            code=40989,
            status_code=409,
            data={"quality_issues": errors},
        )


async def _video_model(db: AsyncSession, model_id: UUID) -> AiModel:
    model = (
        await db.execute(
            select(AiModel).where(
                AiModel.id == model_id,
                AiModel.model_type == "video",
                AiModel.is_enabled.is_(True),
            )
        )
    ).scalar_one_or_none()
    if model is None:
        raise AppException("视频模型不存在或已禁用", code=40404, status_code=404)
    return model


async def _storyboard_video_parameters(
    db: AsyncSession,
    storyboard: ProjectStoryboard,
    default_model: AiModel,
    default_resolution: str,
    *,
    use_saved_config: bool,
    model_cache: Dict[UUID, AiModel],
    requested_duration_seconds: Optional[int] = None,
    expected_config_version: Optional[int] = None,
) -> tuple[AiModel, str, int, int]:
    config = dict((storyboard.extra or {}).get("agent_video_config") or {})
    config_version = int(config.get("version") or 0)
    if not use_saved_config:
        if expected_config_version != config_version:
            raise AppException(
                f"分镜组视频配置版本冲突，当前版本为 {config_version}",
                code=40992,
                status_code=409,
                data={
                    "expected_config_version": expected_config_version,
                    "current_config_version": config_version,
                },
            )
        if requested_duration_seconds is None:
            raise AppException("生成视频需要提交视频时长", code=40037, status_code=400)
        return default_model, default_resolution, requested_duration_seconds, config_version
    configured_model_id = _optional_uuid(config.get("video_model_id"))
    model = default_model
    if configured_model_id is not None:
        cached_model = model_cache.get(configured_model_id)
        if cached_model is None:
            model = await _video_model(db, configured_model_id)
            model_cache[configured_model_id] = model
        else:
            model = cached_model
    resolution = str(config.get("video_resolution") or default_resolution)
    if resolution not in {"480p", "720p", "1080p", "4k"}:
        resolution = default_resolution
    duration_seconds = int(
        config.get("estimated_duration_seconds")
        or storyboard_estimated_duration_seconds(storyboard)
    )
    return model, resolution, duration_seconds, config_version


async def _existing_request(
    db: AsyncSession,
    production_id: UUID,
    idempotency_key: str,
) -> Optional[AgentStoryboardMediaRequest]:
    return (
        await db.execute(
            select(AgentStoryboardMediaRequest).where(
                AgentStoryboardMediaRequest.production_id == production_id,
                AgentStoryboardMediaRequest.media_type == "video",
                AgentStoryboardMediaRequest.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()


def _check_request_replay(
    request: AgentStoryboardMediaRequest,
    signature: str,
    chapter_id: UUID,
) -> None:
    if request.chapter_id != chapter_id or (request.extra or {}).get(
        "request_signature"
    ) != signature:
        raise AppException("幂等键已用于其他视频生成请求", code=40971, status_code=409)


async def _generation_result(
    db: AsyncSession,
    request: AgentStoryboardMediaRequest,
    *,
    idempotent_replay: bool,
) -> Dict[str, Any]:
    raw_items = list(request.items or [])
    task_ids = [
        value
        for value in (_optional_uuid(item.get("task_record_id")) for item in raw_items)
        if value is not None
    ]
    tasks: Dict[UUID, UserTaskRecord] = {}
    if task_ids:
        result = await db.execute(select(UserTaskRecord).where(UserTaskRecord.id.in_(task_ids)))
        tasks = {item.id: item for item in result.scalars().all()}
    items = []
    for raw in raw_items:
        task_id = _optional_uuid(raw.get("task_record_id"))
        task = tasks.get(task_id) if task_id is not None else None
        items.append(
            {
                **raw,
                "task_record_id": task_id,
                "status": task.status if task is not None else str(raw.get("status") or "failed"),
                "next_poll_seconds": (
                    provider_next_poll_seconds(
                        task.generation_type,
                        task.status,
                        task.extra or {},
                    )
                    if task is not None
                    else None
                ),
            }
        )
    return {
        "request_id": request.id,
        "production_id": request.production_id,
        "chapter_id": request.chapter_id,
        "idempotency_key": request.idempotency_key,
        "status": request.status,
        "idempotent_replay": idempotent_replay,
        "submitted_count": sum(bool(item.get("submitted")) for item in items),
        "reused_count": sum(
            not item.get("submitted") and not item.get("error_message")
            for item in items
        ),
        "failed_count": sum(bool(item.get("error_message")) for item in items),
        "total_points_cost": request.submitted_points,
        "items": items,
    }


async def _current_video_tasks(
    db: AsyncSession,
    storyboards: List[ProjectStoryboard],
) -> Dict[UUID, UserTaskRecord]:
    task_ids = [
        value
        for value in (
            _optional_uuid(
                (storyboard.extra or {}).get("video_generation_task_record_id")
            )
            for storyboard in storyboards
        )
        if value is not None
    ]
    if not task_ids:
        return {}
    result = await db.execute(select(UserTaskRecord).where(UserTaskRecord.id.in_(task_ids)))
    return {item.id: item for item in result.scalars().all()}


def _video_item(
    storyboard: ProjectStoryboard,
    tasks: Dict[UUID, UserTaskRecord],
) -> Dict[str, Any]:
    extra = storyboard.extra or {}
    task_id = _optional_uuid(extra.get("video_generation_task_record_id"))
    task = tasks.get(task_id) if task_id is not None else None
    return {
        "storyboard_id": storyboard.id,
        "shot_number": storyboard.shot_number,
        "title": storyboard.title,
        "revision": _storyboard_revision(storyboard),
        "estimated_duration_seconds": storyboard_estimated_duration_seconds(storyboard),
        "video": {
            "status": _video_status(storyboard),
            "result_url": _video_url(storyboard) or None,
            "task_record_id": task_id,
            "model_id": task.ai_model_id if task is not None else None,
            "resolution": (
                str((task.extra or {}).get("resolution") or "") or None
                if task is not None
                else None
            ),
            "next_poll_seconds": (
                provider_next_poll_seconds(task.generation_type, task.status, task.extra or {})
                if task is not None
                else None
            ),
            "selected_history_id": _optional_uuid(
                extra.get("video_generation_history_id")
            ),
            "latest_history_id": _optional_uuid(extra.get("video_latest_history_id")),
            "selection_revision": int(extra.get("video_selection_revision") or 0),
            "selection_required": bool(extra.get("video_selection_required")),
        },
    }


def _video_version_item(history: ProjectGeneratedAsset) -> Dict[str, Any]:
    extra = history.extra or {}
    return {
        "history_id": history.id,
        "task_record_id": history.task_record_id,
        "video_model_id": history.ai_model_id,
        "result_url": history.result_url,
        "result_urls": list(history.result_urls or []),
        "last_frame_url": history.last_frame_url,
        "status": history.status,
        "is_selected": history.is_selected,
        "validity_status": str(extra.get("validity_status") or "current"),
        "resolution": str(extra.get("resolution") or "") or None,
        "requested_duration_seconds": _optional_int(
            extra.get("requested_duration_seconds")
        ),
        "prompt": history.prompt,
        "asset_bindings": list(extra.get("asset_bindings") or []),
        "reference_images": list(extra.get("reference_images") or []),
        "reference_manifest": list(extra.get("reference_manifest") or []),
        "provider_parameters": dict(extra.get("provider_parameters") or {}),
        "storyboard_revision": _optional_int(extra.get("storyboard_revision")),
        "core_asset_lock_version": _optional_int(
            extra.get("core_asset_lock_version")
        ),
        "created_at": history.created_at,
    }


def _request_signature(
    context: EpisodeVideoContext,
    payload: AgentEpisodeVideoGenerationRequest,
) -> str:
    value = {
        "production_id": str(context.production.id),
        "chapter_id": str(context.chapter.id),
        "core_asset_lock_version": payload.expected_core_asset_lock_version,
        "episode_revision": payload.expected_episode_revision,
        "video_model_id": str(payload.video_model_id),
        "video_resolution": payload.video_resolution,
    }
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _storyboard_request_signature(
    context: EpisodeVideoContext,
    storyboard_id: UUID,
    payload: AgentStoryboardVideoGenerationRequest,
) -> str:
    value = {
        "production_id": str(context.production.id),
        "chapter_id": str(context.chapter.id),
        "storyboard_id": str(storyboard_id),
        "core_asset_lock_version": payload.expected_core_asset_lock_version,
        "episode_revision": payload.expected_episode_revision,
        "storyboard_revision": payload.expected_storyboard_revision,
        "video_model_id": str(payload.video_model_id),
        "video_resolution": payload.video_resolution,
        "duration_seconds": payload.duration_seconds,
        "video_config_version": payload.expected_video_config_version,
        "prompt": payload.prompt,
    }
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _status_counts(storyboards: List[ProjectStoryboard]) -> Dict[str, int]:
    counts = {key: 0 for key in VIDEO_STATUS_KEYS}
    for storyboard in storyboards:
        status = _video_status(storyboard)
        counts[status] = counts.get(status, 0) + 1
    return counts


def _video_status(storyboard: ProjectStoryboard) -> str:
    return str((storyboard.extra or {}).get("video_generation_status") or "not_started")


def _video_url(storyboard: ProjectStoryboard) -> str:
    return str((storyboard.extra or {}).get("video_generation_result") or "").strip()


def _storyboard_revision(storyboard: ProjectStoryboard) -> int:
    return max(1, int((storyboard.extra or {}).get("agent_storyboard_revision") or 1))


def _episode_revision(chapter: ProjectChapter) -> int:
    return max(1, int((chapter.extra or {}).get("agent_storyboard_episode_revision") or 1))


def _error_item(storyboard_id: UUID, code: int, message: str) -> Dict[str, Any]:
    return {
        "storyboard_id": str(storyboard_id),
        "submitted": False,
        "reused_active_task": False,
        "task_record_id": None,
        "status": "failed",
        "points_cost": 0,
        "error_code": code,
        "error_message": message,
    }


def _optional_uuid(value: Any) -> Optional[UUID]:
    if value in (None, ""):
        return None
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _optional_int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
