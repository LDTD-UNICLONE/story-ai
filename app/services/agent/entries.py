from pathlib import PurePath
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from fastapi import UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import AppException
from app.core.timezone import beijing_datetime
from app.models.agent_production import AgentEvent, AgentProduction, ProjectSourceDocument
from app.models.ai_model import AiModel
from app.models.agent_review import AgentDelivery
from app.models.project import Project
from app.models.style import Style
from app.models.task_record import UserTaskRecord
from app.models.user import User
from app.schemas.agent_production import (
    AgentProductionConfigurationRequest,
    AgentProductionFromTextRequest,
    AgentProductionMode,
    AgentVideoResolution,
)
from app.schemas.project import ProjectGenerationRatio
from app.services.agent.source_files import (
    PREVIEW_CHARACTERS,
    save_source_document,
    parse_agent_source_file,
)
from app.services.styles import get_enabled_style_or_404
from app.services.uploads import upload_story_file
from app.services.agent.workflow_steps import initialize_agent_workflow_states
from app.services.agent.default_models import get_agent_video_model
from app.services.billing.points import ensure_user_points_enough


AGENT_AUTOMATIC_MINIMUM_POINTS = 100


async def require_agent_automatic_mode_points(
    db: AsyncSession,
    user_id: UUID,
    mode: AgentProductionMode,
) -> None:
    if mode == "automatic":
        await ensure_user_points_enough(
            db,
            user_id,
            AGENT_AUTOMATIC_MINIMUM_POINTS,
        )


async def create_agent_production_from_text(
    db: AsyncSession,
    user: User,
    payload: AgentProductionFromTextRequest,
) -> Dict[str, Any]:
    content = payload.content.strip()
    _validate_content_length(content)
    await require_agent_automatic_mode_points(db, user.id, payload.mode)
    style = await get_enabled_style_or_404(db, payload.style_id)
    video_model = await _initial_video_model(db, payload.mode, payload.video_model_id)
    name = _agent_project_name(payload.name, content=content)
    project = _new_agent_project(
        user_id=user.id,
        name=name,
        style_id=style.id,
        generation_ratio=payload.generation_ratio,
    )
    db.add(project)
    await db.flush()
    source = await save_source_document(
        db,
        project_id=project.id,
        user_id=user.id,
        source_type="text",
        filename="",
        content=content,
        file_url="",
        upload_extra={"warnings": []},
        commit=False,
    )
    return await _finish_creation(
        db,
        user=user,
        project=project,
        source=source,
        mode=payload.mode,
        video_resolution=payload.video_resolution,
        video_model=video_model,
        warnings=[],
    )


async def create_agent_production_from_file(
    db: AsyncSession,
    user: User,
    file: UploadFile,
    *,
    name: Optional[str],
    style_id: UUID,
    generation_ratio: ProjectGenerationRatio,
    video_resolution: AgentVideoResolution,
    mode: AgentProductionMode,
    video_model_id: Optional[UUID],
) -> Dict[str, Any]:
    await require_agent_automatic_mode_points(db, user.id, mode)
    parsed = await parse_agent_source_file(file)
    style = await get_enabled_style_or_404(db, style_id)
    video_model = await _initial_video_model(db, mode, video_model_id)
    project = _new_agent_project(
        user_id=user.id,
        name=_agent_project_name(name, filename=parsed.filename, content=parsed.content),
        style_id=style.id,
        generation_ratio=generation_ratio,
    )
    db.add(project)
    await db.flush()
    try:
        await file.seek(0)
        uploaded = await upload_story_file(file, category=f"agent-source/{project.id}")
        source = await save_source_document(
            db,
            project_id=project.id,
            user_id=user.id,
            source_type=parsed.source_type,
            filename=parsed.filename,
            content=parsed.content,
            file_url=uploaded.url,
            upload_extra={
                "object_key": uploaded.object_key,
                "content_type": uploaded.content_type,
                "file_size": uploaded.size,
                "warnings": parsed.warnings,
            },
            commit=False,
        )
        return await _finish_creation(
            db,
            user=user,
            project=project,
            source=source,
            mode=mode,
            video_resolution=video_resolution,
            video_model=video_model,
            warnings=parsed.warnings,
        )
    except Exception:
        await db.rollback()
        raise


async def list_agent_projects(
    db: AsyncSession,
    user_id: UUID,
    status: Optional[str],
    page: int,
    page_size: int,
) -> Tuple[List[Dict[str, Any]], int]:
    conditions = [
        AgentProduction.user_id == user_id,
        Project.user_id == user_id,
        ProjectSourceDocument.user_id == user_id,
        ProjectSourceDocument.project_id == Project.id,
        Project.project_kind == "agent",
        Project.is_enabled.is_(True),
    ]
    if status:
        conditions.append(AgentProduction.status == status)
    count_result = await db.execute(
        select(func.count())
        .select_from(AgentProduction)
        .join(Project, Project.id == AgentProduction.project_id)
        .join(
            ProjectSourceDocument,
            ProjectSourceDocument.id == AgentProduction.source_document_id,
        )
        .where(*conditions)
    )
    result = await db.execute(
        select(AgentProduction, Project, ProjectSourceDocument)
        .join(Project, Project.id == AgentProduction.project_id)
        .join(
            ProjectSourceDocument,
            ProjectSourceDocument.id == AgentProduction.source_document_id,
        )
        .where(*conditions)
        .order_by(AgentProduction.created_at.desc(), AgentProduction.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = list(result.all())
    production_ids = [production.id for production, _project, _source in rows]
    active_task_counts = await _active_agent_task_counts(db, user_id, production_ids)
    active_delivery_counts = await _active_agent_delivery_counts(
        db,
        user_id,
        production_ids,
    )
    items = []
    for production, project, source in rows:
        active_task_count = active_task_counts.get(
            production.id,
            0,
        ) + active_delivery_counts.get(production.id, 0)
        polling = agent_list_polling_state(production, active_task_count)
        items.append(
            {
                "id": production.id,
                "name": project.name,
                "status": production.status,
                "current_stage": production.current_stage,
                "mode": production.mode,
                "source_type": source.source_type,
                "character_count": source.character_count,
                "style_id": project.style_id,
                "generation_ratio": project.generation_ratio,
                "video_resolution": (production.production_spec or {}).get("video_resolution"),
                "video_model_id": (production.production_spec or {}).get("video_model_id"),
                "configuration_required": not _configuration_confirmed(production, project),
                "consumed_points": production.consumed_points,
                "error_summary": production.error_summary,
                "active_task_count": active_task_count,
                **polling,
                "created_at": production.created_at,
                "updated_at": production.updated_at,
            }
        )
    return items, int(count_result.scalar_one())


def agent_list_polling_state(
    production: AgentProduction,
    active_task_count: int,
) -> Dict[str, Any]:
    has_active_tasks = active_task_count > 0
    automatic_background_work = production.mode == "automatic" and production.status in {
        "planning",
        "running",
    }
    should_poll = has_active_tasks or automatic_background_work
    return {
        "has_active_tasks": has_active_tasks,
        "should_poll": should_poll,
        "next_poll_seconds": 10 if should_poll else None,
    }


async def _active_agent_task_counts(
    db: AsyncSession,
    user_id: UUID,
    production_ids: List[UUID],
) -> Dict[UUID, int]:
    if not production_ids:
        return {}
    production_id = UserTaskRecord.extra["agent_production_id"].as_string()
    result = await db.execute(
        select(production_id, func.count())
        .where(
            UserTaskRecord.user_id == user_id,
            UserTaskRecord.status.in_(("pending", "running")),
            production_id.in_([str(value) for value in production_ids]),
        )
        .group_by(production_id)
    )
    return {
        parsed: int(count)
        for raw_id, count in result.all()
        if (parsed := _optional_uuid(raw_id)) is not None
    }


async def _active_agent_delivery_counts(
    db: AsyncSession,
    user_id: UUID,
    production_ids: List[UUID],
) -> Dict[UUID, int]:
    if not production_ids:
        return {}
    result = await db.execute(
        select(AgentDelivery.production_id, func.count())
        .where(
            AgentDelivery.user_id == user_id,
            AgentDelivery.production_id.in_(production_ids),
            AgentDelivery.status.in_(("pending", "running")),
        )
        .group_by(AgentDelivery.production_id)
    )
    return {production_id: int(count) for production_id, count in result.all()}


async def _finish_creation(
    db: AsyncSession,
    *,
    user: User,
    project: Project,
    source: ProjectSourceDocument,
    mode: AgentProductionMode,
    video_resolution: AgentVideoResolution,
    video_model: Optional[AiModel],
    warnings: List[str],
) -> Dict[str, Any]:
    configured_at = beijing_datetime()
    production_spec = {
        "video_resolution": video_resolution,
        "workflow_version": 2,
        "retry_limit": 0,
        "pilot_episode_count": 0,
    }
    if video_model is not None:
        production_spec["video_model_id"] = str(video_model.id)
        production_spec["video_model_snapshot"] = _video_model_snapshot(video_model)
    production = AgentProduction(
        project_id=project.id,
        user_id=user.id,
        source_document_id=source.id,
        status="draft",
        current_stage="source",
        mode=mode,
        production_spec=production_spec,
        estimated_points=0,
        consumed_points=0,
        max_points=None,
        lock_version=0,
        extra={
            "configuration_confirmed": True,
            "configured_at": configured_at.isoformat(),
        },
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
            payload={
                "source_document_id": str(source.id),
                "source_version": source.version,
                "workflow_version": 2,
                "style_id": str(project.style_id),
                "generation_ratio": project.generation_ratio,
                "video_resolution": video_resolution,
                "mode": mode,
                "video_model_id": str(video_model.id) if video_model is not None else None,
            },
        )
    )
    await db.commit()
    await db.refresh(production)
    preview = source.content[:PREVIEW_CHARACTERS]
    return {
        "production_id": production.id,
        "name": project.name,
        "status": production.status,
        "current_stage": production.current_stage,
        "source": {
            "source_type": source.source_type,
            "file_name": source.file_name or None,
            "content_hash": source.content_hash,
            "character_count": source.character_count,
            "content_preview": preview,
            "content_preview_truncated": len(source.content) > len(preview),
            "warnings": warnings,
        },
        "style_id": project.style_id,
        "generation_ratio": project.generation_ratio,
        "video_resolution": video_resolution,
        "mode": mode,
        "video_model_id": video_model.id if video_model is not None else None,
        "configuration_required": False,
        "created_at": production.created_at,
    }


def _new_agent_project(
    *,
    user_id: UUID,
    name: str,
    style_id: UUID,
    generation_ratio: ProjectGenerationRatio,
) -> Project:
    return Project(
        user_id=user_id,
        style_id=style_id,
        name=name,
        cover="",
        description="",
        generation_ratio=generation_ratio,
        project_kind="agent",
        is_enabled=True,
    )


def _agent_project_name(
    requested_name: Optional[str],
    *,
    filename: str = "",
    content: str,
) -> str:
    if requested_name and requested_name.strip():
        return requested_name.strip()[:128]
    if filename:
        file_stem = PurePath(filename).stem.strip()
        if file_stem:
            return file_stem[:128]
    first_line = next((line.strip() for line in content.splitlines() if line.strip()), "")
    return (first_line or "未命名剧本")[:128]


def _validate_content_length(content: str) -> None:
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


def _optional_uuid(value: Any) -> Optional[UUID]:
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


async def get_agent_production_configuration(
    db: AsyncSession,
    production_id: UUID,
    user_id: UUID,
) -> Dict[str, Any]:
    production, project = await _configuration_context(
        db,
        production_id,
        user_id,
        lock=False,
    )
    style = await db.get(Style, project.style_id) if project.style_id is not None else None
    return _configuration_payload(production, project, style)


async def configure_agent_production(
    db: AsyncSession,
    production_id: UUID,
    user_id: UUID,
    payload: AgentProductionConfigurationRequest,
) -> Dict[str, Any]:
    production, project = await _configuration_context(
        db,
        production_id,
        user_id,
        lock=True,
    )
    if production.status != "draft" or production.current_stage != "source":
        raise AppException("剧本分析启动后不能修改整体配置", code=40981, status_code=409)
    await require_agent_automatic_mode_points(db, user_id, payload.mode)
    style = await get_enabled_style_or_404(db, payload.style_id)
    video_model = await _initial_video_model(db, payload.mode, payload.video_model_id)
    project.style_id = style.id
    project.generation_ratio = payload.generation_ratio
    project.updated_at = beijing_datetime()
    production.mode = payload.mode
    production_spec = {
        **(production.production_spec or {}),
        "video_resolution": payload.video_resolution,
    }
    production_spec.pop("video_model_id", None)
    production_spec.pop("video_model_snapshot", None)
    if video_model is not None:
        production_spec["video_model_id"] = str(video_model.id)
        production_spec["video_model_snapshot"] = _video_model_snapshot(video_model)
    production.production_spec = production_spec
    production.extra = {
        **(production.extra or {}),
        "configuration_confirmed": True,
        "configured_at": beijing_datetime().isoformat(),
    }
    production.lock_version += 1
    production.updated_at = beijing_datetime()
    db.add(
        AgentEvent(
            production_id=production.id,
            actor_user_id=user_id,
            event_type="production.configured",
            source="user",
            payload={
                "style_id": str(style.id),
                "generation_ratio": payload.generation_ratio,
                "video_resolution": payload.video_resolution,
                "mode": payload.mode,
                "video_model_id": str(video_model.id) if video_model is not None else None,
            },
        )
    )
    await db.commit()
    return _configuration_payload(production, project, style)


async def _configuration_context(
    db: AsyncSession,
    production_id: UUID,
    user_id: UUID,
    *,
    lock: bool,
) -> Tuple[AgentProduction, Project]:
    query = (
        select(AgentProduction, Project)
        .join(Project, Project.id == AgentProduction.project_id)
        .where(
            AgentProduction.id == production_id,
            AgentProduction.user_id == user_id,
            Project.user_id == user_id,
            Project.project_kind == "agent",
            Project.is_enabled.is_(True),
        )
    )
    if lock:
        query = query.with_for_update(of=AgentProduction)
    result = await db.execute(query)
    row = result.one_or_none()
    if row is None:
        raise AppException("整剧任务不存在", code=40430, status_code=404)
    return row


def _configuration_payload(
    production: AgentProduction,
    project: Project,
    style: Optional[Style],
) -> Dict[str, Any]:
    configured = _configuration_confirmed(production, project)
    return {
        "production_id": production.id,
        "style_id": project.style_id,
        "style": style,
        "generation_ratio": project.generation_ratio,
        "video_resolution": (production.production_spec or {}).get("video_resolution"),
        "mode": production.mode,
        "video_model_id": (production.production_spec or {}).get("video_model_id"),
        "configured": configured,
        "configurable": production.status == "draft" and production.current_stage == "source",
    }


def _configuration_confirmed(production: AgentProduction, project: Project) -> bool:
    return bool(
        (production.extra or {}).get("configuration_confirmed")
        and project.style_id is not None
        and project.generation_ratio
    )


async def _initial_video_model(
    db: AsyncSession,
    mode: AgentProductionMode,
    video_model_id: Optional[UUID],
) -> Optional[AiModel]:
    if mode == "automatic":
        if video_model_id is None:
            raise AppException("自动模式必须提前选择视频模型", code=40059, status_code=400)
        return await get_agent_video_model(db, video_model_id)
    if video_model_id is not None:
        raise AppException(
            "审核模式应在生成视频时选择视频模型",
            code=40059,
            status_code=400,
        )
    return None


def _video_model_snapshot(model: AiModel) -> Dict[str, str]:
    return {
        "record_id": str(model.id),
        "model_id": model.model_id,
        "nickname": model.nickname,
        "vendor": model.vendor,
    }
