import hashlib
from typing import List, Optional, Tuple
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.exceptions import AppException
from app.core.timezone import beijing_datetime
from app.models.agent_production import (
    AgentEvent,
    AgentProduction,
    AgentStep,
    ProjectSourceDocument,
)
from app.models.ai_model import AiModel
from app.models.project import Project
from app.models.style import Style
from app.models.task_record import UserTaskRecord
from app.models.user import User
from app.schemas.agent_production import (
    AgentProductionAction,
    AgentProductionCreateRequest,
)
from app.services.generation.task_dispatch import (
    dispatch_tasks_best_effort,
    enqueue_task_dispatch,
)
from app.services.agent.production_state import (
    InvalidAgentStateTransition,
    transition_production_status,
    transition_step_status,
)
from app.services.agent.default_models import (
    resolve_agent_default_models,
    resolve_fixed_agent_models,
)
from app.services.agent.workflow import uses_agent_workflow_v2
from app.services.agent.workflow_steps import initialize_agent_workflow_states
from app.services.agent.entries import require_agent_automatic_mode_points
from app.services.projects.queries import get_project_or_404
from app.services.billing.points import change_user_points


async def create_agent_production(
    db: AsyncSession,
    project_id: UUID,
    user: User,
    payload: AgentProductionCreateRequest,
) -> AgentProduction:
    await get_project_or_404(db, project_id, user.id)
    await require_agent_automatic_mode_points(db, user.id, payload.mode)
    await _validate_production_models(db, payload)
    await _lock_project(db, project_id, user.id)
    if payload.source_document_id is not None:
        source_result = await db.execute(
            select(ProjectSourceDocument).where(
                ProjectSourceDocument.id == payload.source_document_id,
                ProjectSourceDocument.project_id == project_id,
                ProjectSourceDocument.user_id == user.id,
            )
        )
        source_document = source_result.scalar_one_or_none()
        if source_document is None:
            raise AppException("预览剧本文档不存在", code=40431, status_code=404)
    else:
        content = (payload.content or "").strip()
        if not content:
            raise AppException("整剧剧本内容不能为空", code=40049, status_code=400)
        if (
            settings.agent_source_max_characters > 0
            and len(content) > settings.agent_source_max_characters
        ):
            raise AppException(
                f"整剧剧本不能超过 {settings.agent_source_max_characters} 个字符",
                code=40050,
                status_code=400,
            )
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        duplicate_result = await db.execute(
            select(ProjectSourceDocument).where(
                ProjectSourceDocument.project_id == project_id,
                ProjectSourceDocument.content_hash == content_hash,
            )
        )
        source_document = duplicate_result.scalar_one_or_none()
        if source_document is None:
            version_result = await db.execute(
                select(func.coalesce(func.max(ProjectSourceDocument.version), 0)).where(
                    ProjectSourceDocument.project_id == project_id
                )
            )
            source_document = ProjectSourceDocument(
                project_id=project_id,
                user_id=user.id,
                source_type=payload.source_type,
                file_url=payload.file_url,
                file_name=payload.file_name,
                content=content,
                content_hash=content_hash,
                character_count=len(content),
                version=int(version_result.scalar_one()) + 1,
                parse_status="draft",
                extra={},
            )
            db.add(source_document)
            await db.flush()
    version = source_document.version

    production = AgentProduction(
        project_id=project_id,
        user_id=user.id,
        source_document_id=source_document.id,
        status="draft",
        current_stage="source",
        mode=payload.mode,
        production_spec=payload.production_spec.model_dump(mode="json"),
        estimated_points=0,
        consumed_points=0,
        max_points=payload.max_points,
        lock_version=0,
        extra={},
    )
    db.add(production)
    await db.flush()
    initialize_agent_workflow_states(db, production)
    db.add(
        AgentEvent(
            production_id=production.id,
            actor_user_id=user.id,
            event_type="production.created",
            source="user",
            payload={"source_document_id": str(source_document.id), "source_version": version},
        )
    )
    await db.commit()
    return await get_agent_production_or_404(db, production.id, user.id)


async def list_agent_productions(
    db: AsyncSession,
    project_id: UUID,
    user_id: UUID,
    status: Optional[str],
    page: int,
    page_size: int,
) -> Tuple[List[AgentProduction], int]:
    await get_project_or_404(db, project_id, user_id)
    conditions = [
        AgentProduction.project_id == project_id,
        AgentProduction.user_id == user_id,
    ]
    if status:
        conditions.append(AgentProduction.status == status)

    count_result = await db.execute(
        select(func.count()).select_from(AgentProduction).where(*conditions)
    )
    total = int(count_result.scalar_one())
    result = await db.execute(
        select(AgentProduction)
        .where(*conditions)
        .order_by(AgentProduction.created_at.desc(), AgentProduction.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(result.scalars().all()), total


async def get_agent_production_or_404(
    db: AsyncSession,
    production_id: UUID,
    user_id: UUID,
) -> AgentProduction:
    result = await db.execute(
        select(AgentProduction)
        .join(Project, Project.id == AgentProduction.project_id)
        .join(
            ProjectSourceDocument,
            ProjectSourceDocument.id == AgentProduction.source_document_id,
        )
        .where(
            AgentProduction.id == production_id,
            AgentProduction.user_id == user_id,
            Project.user_id == user_id,
            Project.is_enabled.is_(True),
            ProjectSourceDocument.project_id == Project.id,
            ProjectSourceDocument.user_id == user_id,
        )
        .options(
            selectinload(AgentProduction.source_document),
            selectinload(AgentProduction.steps),
            selectinload(AgentProduction.checkpoints),
        )
    )
    production = result.scalar_one_or_none()
    if production is None:
        raise AppException("整剧任务不存在", code=40430, status_code=404)
    return production


async def delete_agent_production(
    db: AsyncSession,
    production_id: UUID,
    user: User,
) -> dict:
    production = await _get_locked_production(db, production_id, user.id)
    project_result = await db.execute(
        select(Project)
        .where(
            Project.id == production.project_id,
            Project.user_id == user.id,
            Project.project_kind == "agent",
            Project.is_enabled.is_(True),
        )
        .with_for_update(of=Project)
    )
    project = project_result.scalar_one_or_none()
    if project is None:
        raise AppException("整剧任务不存在", code=40430, status_code=404)

    previous_status = production.status
    if production.status not in {"completed", "cancelled"}:
        await _cancel_production(db, production)
    project.is_enabled = False
    project.updated_at = beijing_datetime()
    production.lock_version += 1
    db.add(
        AgentEvent(
            production_id=production.id,
            actor_user_id=user.id,
            event_type="production.deleted",
            source="user",
            payload={
                "from_status": previous_status,
                "to_status": production.status,
                "project_id": str(project.id),
            },
        )
    )
    await db.commit()
    return {
        "production_id": production.id,
        "status": production.status,
        "deleted": True,
    }


async def apply_agent_production_action(
    db: AsyncSession,
    production_id: UUID,
    user: User,
    action: AgentProductionAction,
) -> AgentProduction:
    production = await _get_locked_production(db, production_id, user.id)
    previous_status = production.status
    step_to_enqueue: Optional[AgentStep] = None
    try:
        if action == "start":
            step_to_enqueue = await _start_production(db, production, user.id)
        elif action == "pause":
            _pause_production(production)
        elif action == "resume":
            step_to_enqueue = await _resume_production(db, production)
        elif action == "cancel":
            await _cancel_production(db, production)
        else:
            raise AppException("未知的整剧任务操作", code=40051, status_code=400)
    except InvalidAgentStateTransition as exc:
        raise AppException(
            f"当前状态 {exc.current} 不允许执行 {action} 操作",
            code=40911,
            status_code=409,
        ) from exc

    if production.status != previous_status:
        production.lock_version += 1
        production.updated_at = beijing_datetime()
        db.add(
            AgentEvent(
                production_id=production.id,
                actor_user_id=user.id,
                event_type={
                    "start": "production.started",
                    "pause": "production.paused",
                    "resume": "production.resumed",
                    "cancel": "production.cancelled",
                }[action],
                source="user",
                payload={"from_status": previous_status, "to_status": production.status},
            )
        )
    dispatch_ids = []
    if (
        step_to_enqueue is not None
        and step_to_enqueue.id is not None
        and step_to_enqueue.stage != "batch_production"
    ):
        dispatch_ids.append(await enqueue_source_analysis(db, production.id, step_to_enqueue.id))
    await db.commit()
    await dispatch_tasks_best_effort(db, dispatch_ids)
    if (
        step_to_enqueue is not None
        and step_to_enqueue.id is not None
        and step_to_enqueue.stage == "batch_production"
    ):
        from app.services.agent.batch_productions import resume_batch_production

        await resume_batch_production(db, production.id, user)
    return await get_agent_production_or_404(db, production.id, user.id)


async def _validate_production_models(
    db: AsyncSession,
    payload: AgentProductionCreateRequest,
) -> None:
    expected_models = [
        (payload.production_spec.text_model_id, "text"),
        (payload.production_spec.image_model_id, "image"),
        (payload.production_spec.video_model_id, "video"),
    ]
    result = await db.execute(
        select(AiModel).where(
            AiModel.id.in_({model_id for model_id, _ in expected_models}),
            AiModel.is_enabled.is_(True),
        )
    )
    models = {model.id: model for model in result.scalars().all()}
    for model_id, expected_type in expected_models:
        model = models.get(model_id)
        if model is None or model.model_type != expected_type:
            raise AppException(
                f"{expected_type} 模型不存在、未启用或类型不匹配",
                code=40404,
                status_code=404,
            )


async def _lock_project(db: AsyncSession, project_id: UUID, user_id: UUID) -> None:
    result = await db.execute(
        select(Project.id)
        .where(
            Project.id == project_id,
            Project.user_id == user_id,
            Project.is_enabled.is_(True),
        )
        .with_for_update(of=Project)
    )
    if result.scalar_one_or_none() is None:
        raise AppException("项目不存在", code=40407, status_code=404)


async def _get_locked_production(
    db: AsyncSession,
    production_id: UUID,
    user_id: UUID,
) -> AgentProduction:
    result = await db.execute(
        select(AgentProduction)
        .join(Project, Project.id == AgentProduction.project_id)
        .join(
            ProjectSourceDocument,
            ProjectSourceDocument.id == AgentProduction.source_document_id,
        )
        .where(
            AgentProduction.id == production_id,
            AgentProduction.user_id == user_id,
            Project.user_id == user_id,
            Project.is_enabled.is_(True),
            ProjectSourceDocument.project_id == Project.id,
            ProjectSourceDocument.user_id == user_id,
        )
        .with_for_update(of=AgentProduction)
    )
    production = result.scalar_one_or_none()
    if production is None:
        raise AppException("整剧任务不存在", code=40430, status_code=404)
    return production


async def _start_production(
    db: AsyncSession,
    production: AgentProduction,
    user_id: UUID,
) -> Optional[AgentStep]:
    if production.status == "completed":
        raise InvalidAgentStateTransition("production", production.status, "planning")
    if production.status in {
        "planning",
        "running",
        "waiting_approval",
        "paused",
        "partially_failed",
    }:
        return None
    if uses_agent_workflow_v2(production):
        await _require_agent_configuration(db, production)
    await require_agent_automatic_mode_points(
        db,
        production.user_id,
        production.mode,
    )
    await _snapshot_default_models(db, production)
    production.status = transition_production_status(production.status, "planning")
    production.current_stage = "source_analysis"

    version_result = await db.execute(
        select(ProjectSourceDocument.version).where(
            ProjectSourceDocument.id == production.source_document_id
        )
    )
    input_version = int(version_result.scalar_one())
    step_result = await db.execute(
        select(AgentStep).where(
            AgentStep.production_id == production.id,
            AgentStep.stage == "source_analysis",
            AgentStep.scope_type == "production",
            AgentStep.scope_id == production.id,
            AgentStep.input_version == input_version,
        )
    )
    step = step_result.scalar_one_or_none()
    if step is None:
        step = AgentStep(
            production_id=production.id,
            stage="source_analysis",
            scope_type="production",
            scope_id=production.id,
            status="queued",
            input_version=input_version,
            progress_current=0,
            progress_total=1,
            attempt_count=0,
            extra={"queued_by_user_id": str(user_id)},
        )
        db.add(step)
        await db.flush()
    return step


async def _require_agent_configuration(
    db: AsyncSession,
    production: AgentProduction,
) -> None:
    if not (production.extra or {}).get("configuration_confirmed"):
        raise AppException(
            "请先选择剧本整体风格、画面比例和使用模式",
            code=40981,
            status_code=409,
        )
    result = await db.execute(
        select(Project.id)
        .join(Style, Style.id == Project.style_id)
        .where(
            Project.id == production.project_id,
            Project.user_id == production.user_id,
            Project.project_kind == "agent",
            Project.is_enabled.is_(True),
            Project.generation_ratio.is_not(None),
            Style.is_enabled.is_(True),
        )
    )
    if result.scalar_one_or_none() is None:
        raise AppException(
            "剧本整体配置不完整或所选风格已停用，请重新配置",
            code=40981,
            status_code=409,
        )


async def _snapshot_default_models(
    db: AsyncSession,
    production: AgentProduction,
) -> None:
    spec = dict(production.production_spec or {})
    if uses_agent_workflow_v2(production):
        fixed_models = await resolve_fixed_agent_models(db)
        spec["text_model_id"] = str(fixed_models["text"].id)
        spec["image_model_id"] = str(fixed_models["image"].id)
        production.production_spec = spec
        return
    field_by_type = {
        "text": "text_model_id",
        "image": "image_model_id",
        "video": "video_model_id",
    }
    missing_types = [
        model_type
        for model_type, field_name in field_by_type.items()
        if not spec.get(field_name)
    ]
    if not missing_types:
        return
    defaults = await resolve_agent_default_models(db, missing_types)
    for model_type, model in defaults.items():
        spec[field_by_type[model_type]] = str(model.id)
    production.production_spec = spec


def _pause_production(production: AgentProduction) -> None:
    if production.status == "paused":
        return
    previous_status = production.status
    production.status = transition_production_status(production.status, "paused")
    production.extra = {
        **(production.extra or {}),
        "paused_from_status": previous_status,
    }


async def _resume_production(
    db: AsyncSession,
    production: AgentProduction,
) -> Optional[AgentStep]:
    if production.status in {"planning", "running"}:
        return None
    if production.status not in {"paused", "partially_failed"}:
        raise InvalidAgentStateTransition("production", production.status, "running")
    production.status = transition_production_status(production.status, "running")
    production.error_summary = None
    stage = (
        "batch_production" if production.current_stage.startswith("batch_") else "source_analysis"
    )
    step_result = await db.execute(
        select(AgentStep)
        .where(
            AgentStep.production_id == production.id,
            AgentStep.stage == stage,
            AgentStep.status.in_(("queued", "running", "failed")),
        )
        .order_by(AgentStep.created_at.desc())
        .limit(1)
    )
    step = step_result.scalar_one_or_none()
    if step is None:
        return None
    if step.status == "failed":
        step.status = transition_step_status(step.status, "queued")
        step.finished_at = None
    step.extra = {**(step.extra or {}), "redispatch_requested": True}
    return step


async def enqueue_source_analysis(
    db: AsyncSession,
    production_id: UUID,
    step_id: UUID,
) -> UUID:
    return await enqueue_task_dispatch(
        db,
        task_name="tasks.agent_source_analysis.run_source_analysis",
        args=(str(production_id), str(step_id)),
        queue="story_ai_text",
    )


async def _cancel_production(db: AsyncSession, production: AgentProduction) -> None:
    if production.status == "cancelled":
        return
    production.status = transition_production_status(production.status, "cancelled")
    step_result = await db.execute(
        select(AgentStep).where(
            AgentStep.production_id == production.id,
            AgentStep.status.in_(("not_started", "queued", "running", "waiting_approval")),
        )
    )
    steps = list(step_result.scalars().all())
    task_record_ids = []
    for step in steps:
        step.status = transition_step_status(step.status, "skipped")
        step.finished_at = beijing_datetime()
        task_record_ids.extend(_agent_step_task_record_ids(step))
    if task_record_ids:
        task_result = await db.execute(
            select(UserTaskRecord)
            .where(
                UserTaskRecord.id.in_(task_record_ids),
                UserTaskRecord.status == "pending",
            )
            .with_for_update()
        )
        for task_record in task_result.scalars().all():
            refund_transaction_id = (task_record.extra or {}).get("refund_transaction_id")
            if task_record.points_cost > 0 and not refund_transaction_id:
                transaction = await change_user_points(
                    db,
                    user_id=task_record.user_id,
                    amount=task_record.points_cost,
                    transaction_type="refund",
                    remark=f"整剧任务取消退回积分：{task_record.title}",
                    auto_commit=False,
                )
                refund_transaction_id = str(transaction.id)
                production.consumed_points = max(
                    0,
                    production.consumed_points - task_record.points_cost,
                )
            task_record.status = "failed"
            task_record.result = "整剧任务已取消"
            task_record.extra = {
                **(task_record.extra or {}),
                "cancelled": True,
                "refund_transaction_id": refund_transaction_id,
            }
    source = await db.get(ProjectSourceDocument, production.source_document_id)
    if source is not None and source.parse_status in {"draft", "running"}:
        source.parse_status = "cancelled"


def _agent_step_task_record_ids(step: AgentStep) -> List[UUID]:
    extra = step.extra or {}
    raw_ids = [
        *((item or {}).get("task_record_id") for item in extra.get("chunks") or []),
        extra.get("asset_analysis_task_record_id"),
        extra.get("merge_task_record_id"),
        extra.get("episode_plan_task_record_id"),
        *dict(extra.get("storyboard_task_ids") or {}).values(),
        *dict(extra.get("image_task_ids") or {}).values(),
        *dict(extra.get("video_task_ids") or {}).values(),
    ]
    ids = []
    for raw_id in raw_ids:
        if not raw_id:
            continue
        try:
            ids.append(UUID(str(raw_id)))
        except ValueError:
            continue
    return ids
