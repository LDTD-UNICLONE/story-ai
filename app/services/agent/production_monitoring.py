from dataclasses import dataclass
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from app.core.exceptions import AppException
from app.core.public_messages import sanitize_public_data, sanitize_public_message
from app.models.agent_production import AgentEvent, AgentProduction, AgentStep
from app.models.project import Project
from app.models.project_chapter import ProjectChapter
from app.models.project_storyboard import ProjectStoryboard
from app.models.task_record import UserTaskRecord
from app.services.agent.workflow import agent_pilot_episode_count


ACTIVE_STATUSES = {"pending", "running"}
SUCCESS_STATUSES = {"success", "selected", "skipped"}
RETRYABLE_STATUSES = {"failed", "invalidated", "skipped"}
SKIPPABLE_STATUSES = {"not_started", "failed", "invalidated"}


@dataclass
class MonitoringContext:
    production: AgentProduction
    chapters: List[ProjectChapter]
    storyboards: List[ProjectStoryboard]
    batch_step: Optional[AgentStep]
    task_records: List[UserTaskRecord]


async def get_agent_production_matrix(
    db: AsyncSession,
    production_id: UUID,
    user_id: UUID,
) -> Dict[str, Any]:
    production = await _get_production(db, production_id, user_id)
    context = await _load_context(db, production)
    return _matrix_response(context)


def _matrix_response(context: MonitoringContext) -> Dict[str, Any]:
    episodes = []
    failed_item_count = 0
    for episode, exceptions in _iter_matrix_episodes(context):
        episodes.append(episode)
        failed_item_count += len(exceptions)
    return {
        "production_id": context.production.id,
        "status": context.production.status,
        "current_stage": context.production.current_stage,
        "total_episode_count": len(episodes),
        "completed_episode_count": sum(_episode_completed(item) for item in episodes),
        "failed_item_count": failed_item_count,
        "active_task_count": sum(task.status in ACTIVE_STATUSES for task in context.task_records),
        "episodes": episodes,
    }


async def list_agent_production_exceptions(
    db: AsyncSession,
    production_id: UUID,
    user_id: UUID,
    *,
    page: int,
    page_size: int,
) -> Tuple[List[Dict[str, Any]], int]:
    production = await _get_production(db, production_id, user_id)
    context = await _load_context(db, production)
    start = (page - 1) * page_size
    items: List[Dict[str, Any]] = []
    total = 0
    for _, exceptions in _iter_matrix_episodes(context):
        for exception in exceptions:
            if start <= total < start + page_size:
                items.append(exception)
            total += 1
    return items, total


async def list_agent_production_events(
    db: AsyncSession,
    production_id: UUID,
    user_id: UUID,
    *,
    page: int,
    page_size: int,
) -> Tuple[List[Dict[str, Any]], int]:
    await _get_production(db, production_id, user_id)
    conditions = [AgentEvent.production_id == production_id]
    count_result = await db.execute(select(func.count()).select_from(AgentEvent).where(*conditions))
    result = await db.execute(
        select(AgentEvent)
        .where(*conditions)
        .order_by(AgentEvent.created_at.desc(), AgentEvent.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = [
        {
            "id": event.id,
            "step_id": event.step_id,
            "actor_user_id": event.actor_user_id,
            "event_type": event.event_type,
            "source": event.source,
            "payload": sanitize_public_data(event.payload or {}),
            "created_at": event.created_at,
        }
        for event in result.scalars().all()
    ]
    return items, int(count_result.scalar_one() or 0)


async def get_agent_production_costs(
    db: AsyncSession,
    production_id: UUID,
    user_id: UUID,
) -> Dict[str, Any]:
    production = await _get_production(db, production_id, user_id)
    conditions = await _production_task_conditions(db, production)
    result = await db.execute(
        select(
            UserTaskRecord.generation_type,
            UserTaskRecord.points_cost,
            UserTaskRecord.extra["refund_transaction_id"],
        ).where(*conditions)
    )
    return _build_costs(production, result.all())


async def get_agent_production_overview(
    db: AsyncSession,
    production: AgentProduction,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Build workbench matrix and costs from one context for an already authorized production."""
    context = await _load_context(db, production)
    costs = _build_costs(
        production,
        (
            (
                task.generation_type,
                task.points_cost,
                (task.extra or {}).get("refund_transaction_id"),
            )
            for task in context.task_records
        ),
    )
    return _matrix_response(context), costs


def _build_costs(
    production: AgentProduction,
    tasks: Iterable[Tuple[str, int, Any]],
) -> Dict[str, Any]:
    grouped: Dict[str, Dict[str, int]] = {}
    for generation_type, points_cost, refund_transaction_id in tasks:
        stage = _cost_stage(generation_type)
        item = grouped.setdefault(
            stage,
            {"task_count": 0, "charged_points": 0, "refunded_points": 0},
        )
        item["task_count"] += 1
        item["charged_points"] += points_cost
        if refund_transaction_id:
            item["refunded_points"] += points_cost
    stages = [
        {
            "stage": stage,
            **values,
            "net_points": values["charged_points"] - values["refunded_points"],
        }
        for stage, values in sorted(grouped.items())
    ]
    charged = sum(item["charged_points"] for item in stages)
    refunded = sum(item["refunded_points"] for item in stages)
    return {
        "production_id": production.id,
        "charged_points": charged,
        "refunded_points": refunded,
        "net_points": charged - refunded,
        "production_consumed_points": production.consumed_points,
        "task_count": sum(item["task_count"] for item in stages),
        "stages": stages,
    }


async def _load_context(
    db: AsyncSession,
    production: AgentProduction,
) -> MonitoringContext:
    user_id = production.user_id
    chapter_result = await db.execute(
        select(ProjectChapter)
        .options(load_only(ProjectChapter.id, ProjectChapter.title, ProjectChapter.extra))
        .where(
            ProjectChapter.project_id == production.project_id,
            ProjectChapter.user_id == user_id,
            ProjectChapter.is_enabled.is_(True),
            ProjectChapter.extra["agent_production_id"].as_string() == str(production.id),
        )
        .order_by(ProjectChapter.sort_order, ProjectChapter.created_at, ProjectChapter.id)
    )
    chapters = list(chapter_result.scalars().all())
    storyboards: List[ProjectStoryboard] = []
    if chapters:
        storyboard_result = await db.execute(
            select(ProjectStoryboard)
            .options(
                load_only(
                    ProjectStoryboard.id,
                    ProjectStoryboard.chapter_id,
                    ProjectStoryboard.shot_number,
                    ProjectStoryboard.title,
                    ProjectStoryboard.extra,
                )
            )
            .where(
                ProjectStoryboard.project_id == production.project_id,
                ProjectStoryboard.user_id == user_id,
                ProjectStoryboard.chapter_id.in_([chapter.id for chapter in chapters]),
                ProjectStoryboard.is_enabled.is_(True),
            )
            .order_by(
                ProjectStoryboard.chapter_id,
                ProjectStoryboard.shot_number,
                ProjectStoryboard.created_at,
            )
        )
        storyboards = list(storyboard_result.scalars().all())
    step_result = await db.execute(
        select(AgentStep)
        .options(load_only(AgentStep.extra))
        .where(
            AgentStep.production_id == production.id,
            AgentStep.stage == "batch_production",
        )
        .order_by(AgentStep.created_at.desc())
        .limit(1)
    )
    task_records = await _production_task_records(db, production)
    return MonitoringContext(
        production=production,
        chapters=chapters,
        storyboards=storyboards,
        batch_step=step_result.scalar_one_or_none(),
        task_records=task_records,
    )


async def _get_production(
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


async def _production_task_records(
    db: AsyncSession,
    production: AgentProduction,
) -> List[UserTaskRecord]:
    conditions = await _production_task_conditions(db, production)
    result = await db.execute(
        select(UserTaskRecord)
        .options(
            load_only(
                UserTaskRecord.id,
                UserTaskRecord.generation_type,
                UserTaskRecord.status,
                UserTaskRecord.points_cost,
                UserTaskRecord.extra,
                UserTaskRecord.result,
            )
        )
        .where(*conditions)
        .order_by(UserTaskRecord.created_at, UserTaskRecord.id)
    )
    return list(result.scalars().all())


async def _production_task_conditions(db: AsyncSession, production: AgentProduction) -> list:
    ownership = [UserTaskRecord.extra["agent_production_id"].as_string() == str(production.id)]
    legacy_task_ids = await _legacy_agent_task_ids(db, production.id)
    if legacy_task_ids:
        ownership.append(UserTaskRecord.id.in_(legacy_task_ids))
    return [
        UserTaskRecord.user_id == production.user_id,
        UserTaskRecord.business_id == production.project_id,
        or_(*ownership),
    ]


async def _legacy_agent_task_ids(db: AsyncSession, production_id: UUID) -> List[UUID]:
    result = await db.execute(
        select(AgentStep.extra).where(AgentStep.production_id == production_id)
    )
    task_ids: List[UUID] = []
    for extra in result.scalars().all():
        for field in ("storyboard_task_ids", "image_task_ids", "video_task_ids"):
            for value in ((extra or {}).get(field) or {}).values():
                try:
                    task_ids.append(UUID(str(value)))
                except (TypeError, ValueError):
                    continue
    return list(dict.fromkeys(task_ids))


def _iter_matrix_episodes(
    context: MonitoringContext,
) -> Iterator[Tuple[Dict[str, Any], List[Dict[str, Any]]]]:
    task_index, attempt_counts = _task_indexes(context.task_records)
    by_chapter: Dict[UUID, List[ProjectStoryboard]] = {}
    for storyboard in context.storyboards:
        by_chapter.setdefault(storyboard.chapter_id, []).append(storyboard)
    pilot_count = agent_pilot_episode_count(context.production)
    max_attempts = 1 + max(
        0,
        int((context.production.production_spec or {}).get("retry_limit") or 0),
    )
    issues = (
        list((context.batch_step.extra or {}).get("quality_issues") or [])
        if context.batch_step
        else []
    )
    issue_by_storyboard = {
        str(issue.get("storyboard_id")): issue
        for issue in issues
        if issue.get("severity") == "error" and issue.get("storyboard_id")
    }
    can_operate = context.production.status not in {"paused", "completed", "cancelled"}
    for index, chapter in enumerate(context.chapters):
        exceptions: List[Dict[str, Any]] = []
        chapter_storyboards = by_chapter.get(chapter.id, [])
        is_pilot = index < pilot_count
        storyboard_stage = _stage_state(
            stage="storyboard",
            scope_id=chapter.id,
            extra=chapter.extra or {},
            task_index=task_index,
            attempt_counts=attempt_counts,
            max_attempts=max_attempts,
            can_operate=can_operate and not is_pilot,
            can_skip_scope=not chapter_storyboards,
        )
        storyboard_items = []
        for storyboard in chapter_storyboards:
            issue = issue_by_storyboard.get(str(storyboard.id))
            image_stage = _stage_state(
                stage="image",
                scope_id=storyboard.id,
                extra=storyboard.extra or {},
                task_index=task_index,
                attempt_counts=attempt_counts,
                max_attempts=max_attempts,
                can_operate=can_operate and not is_pilot,
                issue=issue,
            )
            video_stage = _stage_state(
                stage="video",
                scope_id=storyboard.id,
                extra=storyboard.extra or {},
                task_index=task_index,
                attempt_counts=attempt_counts,
                max_attempts=max_attempts,
                can_operate=can_operate and not is_pilot,
            )
            storyboard_items.append(
                {
                    "storyboard_id": storyboard.id,
                    "shot_number": storyboard.shot_number,
                    "title": storyboard.title,
                    "image": image_stage,
                    "video": video_stage,
                }
            )
            for stage_state in (image_stage, video_stage):
                exception = _stage_exception(
                    stage_state,
                    chapter_id=chapter.id,
                    storyboard_id=storyboard.id,
                )
                if exception:
                    exceptions.append(exception)
        episode = {
            "chapter_id": chapter.id,
            "episode_number": int((chapter.extra or {}).get("episode_number") or index + 1),
            "title": chapter.title,
            "is_pilot": is_pilot,
            "storyboard": storyboard_stage,
            "storyboards": storyboard_items,
        }
        exception = _stage_exception(storyboard_stage, chapter_id=chapter.id)
        if exception:
            exceptions.append(exception)
        yield episode, exceptions


def _task_indexes(
    tasks: List[UserTaskRecord],
) -> Tuple[Dict[Tuple[str, UUID], UserTaskRecord], Dict[Tuple[str, UUID], int]]:
    latest: Dict[Tuple[str, UUID], UserTaskRecord] = {}
    counts: Dict[Tuple[str, UUID], int] = {}
    for task in tasks:
        stage = _task_stage(task.generation_type)
        scope_id = _task_scope_id(task, stage)
        if stage is None or scope_id is None:
            continue
        key = (stage, scope_id)
        latest[key] = task
        counts[key] = counts.get(key, 0) + 1
    return latest, counts


def _stage_state(
    *,
    stage: str,
    scope_id: UUID,
    extra: Dict[str, Any],
    task_index: Dict[Tuple[str, UUID], UserTaskRecord],
    attempt_counts: Dict[Tuple[str, UUID], int],
    max_attempts: int,
    can_operate: bool,
    can_skip_scope: bool = True,
    issue: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    prefix = "storyboard_analysis" if stage == "storyboard" else f"{stage}_generation"
    status = str(extra.get(f"{prefix}_status") or "not_started")
    task = task_index.get((stage, scope_id))
    raw_task_id = extra.get(f"{prefix}_task_record_id")
    task_id = _optional_uuid(raw_task_id) or (task.id if task else None)
    error = (
        (issue or {}).get("message")
        or extra.get(f"{prefix}_failed_reason")
        or extra.get(f"{prefix}_error")
        or ((task.extra or {}).get("failed_reason") if task else None)
        or (task.result if task and task.status == "failed" else None)
    )
    resolution = dict((extra.get("agent_manual_resolutions") or {}).get(stage) or {})
    manually_resolved = bool(resolution)
    blocked = issue is not None
    attempt_count = attempt_counts.get((stage, scope_id), 0)
    can_retry = can_operate and not blocked and status in RETRYABLE_STATUSES
    return {
        "stage": stage,
        "status": status,
        "task_record_id": task_id,
        "task_status": task.status if task else None,
        "attempt_count": attempt_count,
        "points_cost": task.points_cost if task else 0,
        "error_message": sanitize_public_message(str(error)) if error else None,
        "can_retry": can_retry,
        "requires_retry_confirmation": can_retry and attempt_count >= max_attempts,
        "can_skip": can_operate
        and can_skip_scope
        and (status in SKIPPABLE_STATUSES or (stage == "video" and status in SUCCESS_STATUSES)),
        "manually_resolved": manually_resolved,
        "replacement_url": resolution.get("replacement_url"),
    }


def _stage_exception(
    stage: Dict[str, Any],
    *,
    chapter_id: UUID,
    storyboard_id: Optional[UUID] = None,
) -> Optional[Dict[str, Any]]:
    if stage["status"] not in {"failed", "invalidated"} and not stage["error_message"]:
        return None
    scope_id = storyboard_id or chapter_id
    message = stage["error_message"] or "上游内容已变更，需要重新生成"
    return {
        "exception_key": f"{stage['stage']}:{scope_id}",
        "category": _exception_category(message, stage["status"]),
        "stage": stage["stage"],
        "scope_type": "storyboard" if storyboard_id else "chapter",
        "scope_id": scope_id,
        "chapter_id": chapter_id,
        "storyboard_id": storyboard_id,
        "task_record_id": stage["task_record_id"],
        "message": message,
        "attempt_count": stage["attempt_count"],
        "can_retry": stage["can_retry"],
        "requires_retry_confirmation": stage["requires_retry_confirmation"],
        "can_skip": stage["can_skip"],
    }


def _episode_completed(episode: Dict[str, Any]) -> bool:
    if episode["storyboard"]["status"] == "skipped":
        return True
    storyboards = episode["storyboards"]
    return bool(storyboards) and all(
        item["video"]["status"] in SUCCESS_STATUSES for item in storyboards
    )


def _task_stage(generation_type: str) -> Optional[str]:
    return {
        "storyboard_analysis": "storyboard",
        "storyboard_image": "image",
        "storyboard_video": "video",
    }.get(generation_type)


def _task_scope_id(task: UserTaskRecord, stage: Optional[str]) -> Optional[UUID]:
    if stage is None:
        return None
    key = "chapter_id" if stage == "storyboard" else "storyboard_id"
    return _optional_uuid((task.extra or {}).get(key))


def _cost_stage(generation_type: str) -> str:
    if generation_type.startswith("agent_"):
        return "source_analysis"
    return _task_stage(generation_type) or "other"


def _exception_category(message: str, status: str) -> str:
    normalized = message.lower()
    if status == "invalidated":
        return "upstream_changed"
    if any(value in normalized for value in ("审核", "合规", "safety", "moderation")):
        return "content_moderation"
    if "积分" in normalized:
        return "insufficient_points"
    if any(value in normalized for value in ("队列", "上限")):
        return "queue_limit"
    if any(value in normalized for value in ("绑定", "锁定", "资产")):
        return "asset_binding"
    if any(value in normalized for value in ("图片", "首帧", "参考图")):
        return "missing_image"
    if any(value in normalized for value in ("模型", "provider", "平台")):
        return "model_failure"
    return "task_failure"


def _optional_uuid(value: Any) -> Optional[UUID]:
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None
