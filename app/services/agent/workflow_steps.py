from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException
from app.core.timezone import beijing_datetime
from app.models.agent_core_asset import AgentCoreAssetLock
from app.models.agent_production import AgentCheckpoint, AgentProduction, AgentStep
from app.models.agent_review import AgentDelivery, AgentEpisodeReview
from app.models.agent_workflow import AgentWorkflowStepState
from app.models.project import Project
from app.models.project_chapter import ProjectChapter
from app.models.project_storyboard import ProjectStoryboard
from app.services.agent.storyboard_episode_state import (
    effective_storyboard_episode_status,
)


PRODUCT_STEP_CODES = {
    1: "script_processing",
    2: "asset_confirmation",
    3: "storyboard_generation",
    4: "video_editing",
}
PRODUCT_STEP_TITLES = {
    1: "剧本处理",
    2: "资产确认",
    3: "分镜生成",
    4: "视频制作",
}
ACTIVE_STATUSES = {"pending", "running"}
SCRIPT_WAITING_STAGES = {"script_review", "episode_plan_review", "story_bible_review"}
SCRIPT_PROCESSING_STAGES = {
    "source_analysis",
    "episode_planning",
    "story_bible",
    "asset_extraction",
}


def current_product_step_number(statuses: Dict[int, str]) -> int:
    for step_number in PRODUCT_STEP_CODES:
        if statuses.get(step_number) != "completed":
            return step_number
    return 4


def initialize_agent_workflow_states(
    db: AsyncSession,
    production: AgentProduction,
) -> None:
    for step_number, step_code in PRODUCT_STEP_CODES.items():
        db.add(
            AgentWorkflowStepState(
                production_id=production.id,
                scope_type="production",
                scope_id=production.id,
                step_number=step_number,
                step_code=step_code,
                status="not_started",
                extra={"current_stage": production.current_stage},
            )
        )


def initialize_agent_episode_workflow_states(
    db: AsyncSession,
    production: AgentProduction,
    chapter: ProjectChapter,
    episode_number: int,
) -> None:
    now = beijing_datetime()
    for step_number, step_code in PRODUCT_STEP_CODES.items():
        db.add(
            AgentWorkflowStepState(
                production_id=production.id,
                scope_type="episode",
                scope_id=chapter.id,
                step_number=step_number,
                step_code=step_code,
                status="completed" if step_number == 1 else "not_started",
                started_at=now if step_number == 1 else None,
                completed_at=now if step_number == 1 else None,
                extra={"episode_number": episode_number},
            )
        )


async def get_agent_workflow(
    db: AsyncSession,
    production_id: UUID,
    user_id: UUID,
) -> Dict[str, Any]:
    production = await _get_agent_production_or_404(db, production_id, user_id)
    chapters = await _chapters(db, production)
    core_lock = await _active_core_lock(db, production.id)
    has_confirmed_script = await _has_confirmed_script_package(db, production.id)
    batch_step = await _latest_batch_step(db, production.id)
    storyboards = await _storyboards(db, production, chapters)
    reviews = await _episode_reviews(db, production.id)
    has_completed_delivery = await _has_completed_full_jianying_export(
        db,
        production.id,
        {chapter.id for chapter in chapters},
    )

    storyboards_by_chapter: Dict[UUID, List[ProjectStoryboard]] = {
        chapter.id: [] for chapter in chapters
    }
    for storyboard in storyboards:
        storyboards_by_chapter.setdefault(storyboard.chapter_id, []).append(storyboard)
    reviews_by_chapter = {review.chapter_id: review for review in reviews}

    step_one_status = _script_processing_status(production)
    step_two_status = _asset_confirmation_status(
        production,
        step_one_status,
        core_lock,
    )
    episode_statuses: Dict[UUID, Dict[int, str]] = {}
    for chapter in chapters:
        chapter_step_one = _episode_script_status(chapter)
        chapter_step_two = step_two_status if core_lock is not None else "not_started"
        chapter_step_three = _episode_storyboard_status(
            chapter,
            storyboards_by_chapter.get(chapter.id, []),
            chapter_step_two,
        )
        chapter_step_four = _episode_video_editing_status(
            chapter_step_three,
            reviews_by_chapter.get(chapter.id),
        )
        episode_statuses[chapter.id] = {
            1: chapter_step_one,
            2: chapter_step_two,
            3: chapter_step_three,
            4: chapter_step_four,
        }

    step_three_status = _storyboard_generation_status(
        production,
        step_two_status,
        batch_step,
        [statuses[3] for statuses in episode_statuses.values()],
    )
    step_four_status = _video_editing_status(
        step_three_status,
        has_completed_delivery,
        [statuses[3] for statuses in episode_statuses.values()],
    )
    production_statuses = {
        1: step_one_status,
        2: step_two_status,
        3: step_three_status,
        4: step_four_status,
    }
    current_step = current_product_step_number(production_statuses)
    production_visibility = {
        1: True,
        2: step_one_status == "completed" or has_confirmed_script or core_lock is not None,
        3: core_lock is not None,
        4: any(statuses[3] == "completed" for statuses in episode_statuses.values()),
    }

    existing = await _existing_states(db, production.id)
    production_records = []
    for step_number, status in production_statuses.items():
        production_records.append(
            _upsert_state(
                db,
                existing,
                production,
                "production",
                production.id,
                step_number,
                status,
                {"current_stage": production.current_stage},
            )
        )

    episode_records: Dict[UUID, List[AgentWorkflowStepState]] = {}
    for chapter in chapters:
        records = []
        statuses = episode_statuses[chapter.id]
        for step_number, status in statuses.items():
            records.append(
                _upsert_state(
                    db,
                    existing,
                    production,
                    "episode",
                    chapter.id,
                    step_number,
                    status,
                    {"episode_number": _episode_number(chapter)},
                )
            )
        episode_records[chapter.id] = records
    await db.commit()

    return {
        "production_id": production.id,
        "mode": production.mode,
        "current_step": current_step,
        "steps": [
            _state_payload(record, current_step, production_visibility[record.step_number])
            for record in production_records
        ],
        "episodes": [
            {
                "chapter_id": chapter.id,
                "episode_number": _episode_number(chapter),
                "title": chapter.title,
                "steps": [
                    _state_payload(
                        record,
                        current_step,
                        production_visibility[record.step_number]
                        if record.step_number < 4
                        else episode_statuses[chapter.id][3] == "completed",
                    )
                    for record in episode_records[chapter.id]
                ],
            }
            for chapter in chapters
        ],
    }


async def require_agent_step_access(
    db: AsyncSession,
    production_id: UUID,
    user_id: UUID,
    requested_step: int,
) -> Dict[str, Any]:
    workflow = await get_agent_workflow(db, production_id, user_id)
    current_step = int(workflow["current_step"])
    requested = next(
        (item for item in workflow["steps"] if item["step_number"] == requested_step),
        None,
    )
    if requested is None or not requested["can_view"]:
        raise AppException(
            "前置步骤尚未完成",
            code=40990,
            status_code=409,
            data={
                "requested_step": requested_step,
                "required_step": requested_step - 1,
                "current_step": current_step,
                "required_status": "completed",
            },
        )
    return workflow


async def _get_agent_production_or_404(
    db: AsyncSession,
    production_id: UUID,
    user_id: UUID,
) -> AgentProduction:
    result = await db.execute(
        select(AgentProduction)
        .join(Project, Project.id == AgentProduction.project_id)
        .where(
            AgentProduction.id == production_id,
            AgentProduction.user_id == user_id,
            Project.user_id == user_id,
            Project.is_enabled.is_(True),
        )
    )
    production = result.scalar_one_or_none()
    if production is None:
        raise AppException("整剧任务不存在", code=40430, status_code=404)
    return production


def _script_processing_status(production: AgentProduction) -> str:
    stage = production.current_stage
    if stage == "source":
        return "not_started" if production.status == "draft" else "processing"
    if stage in SCRIPT_WAITING_STAGES:
        return "waiting_review"
    if stage in SCRIPT_PROCESSING_STAGES:
        return "failed" if production.status == "partially_failed" else "processing"
    return "completed"


def _asset_confirmation_status(
    production: AgentProduction,
    step_one_status: str,
    core_lock: Optional[AgentCoreAssetLock],
) -> str:
    if core_lock is not None:
        return "completed"
    if step_one_status != "completed":
        return "not_started"
    if production.current_stage == "core_asset_change_review":
        return "invalidated"
    return "waiting_review" if production.current_stage == "core_assets" else "not_started"


def _episode_script_status(chapter: ProjectChapter) -> str:
    if chapter.process_status == "failed":
        return "failed"
    if chapter.process_status in {"success", "completed"} and (
        chapter.processed_content or ""
    ).strip():
        return "completed"
    if chapter.process_status in ACTIVE_STATUSES:
        return "processing"
    return "not_started"


def _episode_storyboard_status(
    chapter: ProjectChapter,
    storyboards: List[ProjectStoryboard],
    asset_status: str,
) -> str:
    if asset_status == "invalidated":
        return "invalidated"
    if asset_status != "completed":
        return "not_started"
    analysis_status = effective_storyboard_episode_status(chapter, len(storyboards))
    if analysis_status == "failed":
        return "failed"
    if analysis_status in ACTIVE_STATUSES:
        return "processing"
    if analysis_status in {"stale", "invalid"}:
        return "invalidated"
    return "completed" if analysis_status == "ready" else "not_started"


def _episode_video_editing_status(
    storyboard_status: str,
    review: Optional[AgentEpisodeReview],
) -> str:
    if review is not None:
        return "completed" if review.status == "approved" else "invalidated"
    if storyboard_status == "invalidated":
        return "invalidated"
    if storyboard_status == "completed":
        return "waiting_review"
    return "not_started"


def _storyboard_generation_status(
    production: AgentProduction,
    asset_status: str,
    batch_step: Optional[AgentStep],
    episode_statuses: List[str],
) -> str:
    if asset_status == "invalidated":
        return "invalidated"
    if asset_status != "completed":
        return "not_started"
    phase = str((batch_step.extra or {}).get("phase") or "") if batch_step else ""
    if phase == "completed" or production.current_stage == "completed":
        return "completed"
    if episode_statuses and all(status == "completed" for status in episode_statuses):
        return "completed"
    if any(status == "processing" for status in episode_statuses):
        return "processing"
    if any(status == "waiting_review" for status in episode_statuses):
        return "waiting_review"
    if any(status == "failed" for status in episode_statuses):
        return "failed"
    if production.current_stage == "batch_production" or production.current_stage.startswith(
        "batch_"
    ):
        return "processing"
    return "not_started"


def _video_editing_status(
    step_three_status: str,
    has_completed_delivery: bool,
    episode_storyboard_statuses: Optional[List[str]] = None,
) -> str:
    if step_three_status == "invalidated":
        return "invalidated"
    if has_completed_delivery:
        return "completed"
    if step_three_status == "completed" or any(
        status == "completed" for status in episode_storyboard_statuses or []
    ):
        return "waiting_review"
    return "not_started"


async def _chapters(
    db: AsyncSession,
    production: AgentProduction,
) -> List[ProjectChapter]:
    result = await db.execute(
        select(ProjectChapter)
        .where(
            ProjectChapter.project_id == production.project_id,
            ProjectChapter.user_id == production.user_id,
            ProjectChapter.is_enabled.is_(True),
            ProjectChapter.extra["agent_production_id"].as_string() == str(production.id),
        )
        .order_by(ProjectChapter.sort_order, ProjectChapter.created_at, ProjectChapter.id)
    )
    return list(result.scalars().all())


async def _active_core_lock(
    db: AsyncSession,
    production_id: UUID,
) -> Optional[AgentCoreAssetLock]:
    result = await db.execute(
        select(AgentCoreAssetLock)
        .where(
            AgentCoreAssetLock.production_id == production_id,
            AgentCoreAssetLock.status == "active",
        )
        .order_by(AgentCoreAssetLock.version.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _has_confirmed_script_package(
    db: AsyncSession,
    production_id: UUID,
) -> bool:
    result = await db.execute(
        select(AgentCheckpoint.id)
        .where(
            AgentCheckpoint.production_id == production_id,
            AgentCheckpoint.checkpoint_type == "script_review",
            AgentCheckpoint.status == "approved",
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def _latest_batch_step(
    db: AsyncSession,
    production_id: UUID,
) -> Optional[AgentStep]:
    result = await db.execute(
        select(AgentStep)
        .where(
            AgentStep.production_id == production_id,
            AgentStep.stage == "batch_production",
            AgentStep.scope_type == "production",
        )
        .order_by(AgentStep.created_at.desc(), AgentStep.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _storyboards(
    db: AsyncSession,
    production: AgentProduction,
    chapters: List[ProjectChapter],
) -> List[ProjectStoryboard]:
    if not chapters:
        return []
    result = await db.execute(
        select(ProjectStoryboard)
        .where(
            ProjectStoryboard.project_id == production.project_id,
            ProjectStoryboard.user_id == production.user_id,
            ProjectStoryboard.chapter_id.in_([chapter.id for chapter in chapters]),
            ProjectStoryboard.is_enabled.is_(True),
        )
        .order_by(ProjectStoryboard.chapter_id, ProjectStoryboard.shot_number)
    )
    return list(result.scalars().all())


async def _episode_reviews(
    db: AsyncSession,
    production_id: UUID,
) -> List[AgentEpisodeReview]:
    result = await db.execute(
        select(AgentEpisodeReview).where(AgentEpisodeReview.production_id == production_id)
    )
    return list(result.scalars().all())


async def _has_completed_full_jianying_export(
    db: AsyncSession,
    production_id: UUID,
    chapter_ids: set[UUID],
) -> bool:
    if not chapter_ids:
        return False
    result = await db.execute(
        select(AgentDelivery)
        .where(
            AgentDelivery.production_id == production_id,
            AgentDelivery.delivery_type == "jianying_draft",
            AgentDelivery.status == "completed",
        )
        .order_by(AgentDelivery.finished_at.desc(), AgentDelivery.id.desc())
    )
    for delivery in result.scalars().all():
        delivered_ids = {
            UUID(str(episode["chapter_id"]))
            for episode in (delivery.manifest or {}).get("episodes") or []
            if episode.get("chapter_id")
        }
        if delivered_ids == chapter_ids:
            return True
    return False


async def _existing_states(
    db: AsyncSession,
    production_id: UUID,
) -> Dict[Tuple[str, UUID, int], AgentWorkflowStepState]:
    result = await db.execute(
        select(AgentWorkflowStepState).where(
            AgentWorkflowStepState.production_id == production_id
        )
    )
    return {
        (state.scope_type, state.scope_id, state.step_number): state
        for state in result.scalars().all()
    }


def _upsert_state(
    db: AsyncSession,
    existing: Dict[Tuple[str, UUID, int], AgentWorkflowStepState],
    production: AgentProduction,
    scope_type: str,
    scope_id: UUID,
    step_number: int,
    status: str,
    extra: Dict[str, Any],
) -> AgentWorkflowStepState:
    key = (scope_type, scope_id, step_number)
    state = existing.get(key)
    now = beijing_datetime()
    if state is None:
        state = AgentWorkflowStepState(
            production_id=production.id,
            scope_type=scope_type,
            scope_id=scope_id,
            step_number=step_number,
            step_code=PRODUCT_STEP_CODES[step_number],
            status=status,
            started_at=now if status != "not_started" else None,
            completed_at=now if status == "completed" else None,
            extra=extra,
        )
        db.add(state)
        existing[key] = state
        return state
    state.step_code = PRODUCT_STEP_CODES[step_number]
    state.status = status
    state.extra = extra
    if status != "not_started" and state.started_at is None:
        state.started_at = now
    if status == "completed" and state.completed_at is None:
        state.completed_at = now
    elif status != "completed":
        state.completed_at = None
    return state


def _state_payload(
    state: AgentWorkflowStepState,
    current_step: int,
    can_view: bool,
) -> Dict[str, Any]:
    return {
        "id": state.id,
        "step_number": state.step_number,
        "step_code": state.step_code,
        "title": PRODUCT_STEP_TITLES[state.step_number],
        "status": state.status,
        "completed": state.status == "completed",
        "can_view": can_view,
        "is_current": state.step_number == current_step,
        "started_at": state.started_at,
        "completed_at": state.completed_at,
        "extra": state.extra or {},
    }


def _episode_number(chapter: ProjectChapter) -> int:
    return int((chapter.extra or {}).get("episode_number") or chapter.sort_order or 0)
