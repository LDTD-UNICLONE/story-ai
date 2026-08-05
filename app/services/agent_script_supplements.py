from typing import Any, Dict, Optional
from uuid import UUID

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import AppException
from app.core.timezone import beijing_datetime
from app.models.agent_production import (
    AgentCheckpoint,
    AgentEvent,
    AgentProduction,
    AgentStep,
    ProjectSourceDocument,
)
from app.models.agent_story_bible import SeriesBibleVersion
from app.models.project import Project
from app.models.user import User
from app.services.agent_production_state import (
    transition_production_status,
    transition_step_status,
)
from app.services.agent_productions import (
    _enqueue_source_analysis,
    get_agent_production_or_404,
)
from app.services.agent_source_files import (
    _save_source_document,
    parse_agent_source_file,
)
from app.services.uploads import upload_story_file


async def append_agent_script_text(
    db: AsyncSession,
    production_id: UUID,
    user: User,
    content: str,
) -> Dict[str, Any]:
    return await _append_agent_script(
        db,
        production_id,
        user,
        content=content,
        source_type="text",
        filename="",
        file_url="",
        upload_extra={},
    )


async def append_agent_script_file(
    db: AsyncSession,
    production_id: UUID,
    user: User,
    file: UploadFile,
) -> Dict[str, Any]:
    production = await get_agent_production_or_404(db, production_id, user.id)
    if production.mode != "supervised":
        raise AppException("自动模式不支持人工补充剧本", code=40982, status_code=409)
    if production.status in {"draft", "paused", "cancelled"}:
        raise AppException("当前整剧状态不允许补充剧本", code=40983, status_code=409)
    parsed = await parse_agent_source_file(file)
    await file.seek(0)
    uploaded = await upload_story_file(
        file,
        category=f"agent-source/{production_id}/supplements",
    )
    return await _append_agent_script(
        db,
        production_id,
        user,
        content=parsed.content,
        source_type=parsed.source_type,
        filename=parsed.filename,
        file_url=uploaded.url,
        upload_extra={
            "object_key": uploaded.object_key,
            "content_type": uploaded.content_type,
            "file_size": uploaded.size,
            "warnings": parsed.warnings,
        },
    )


async def _append_agent_script(
    db: AsyncSession,
    production_id: UUID,
    user: User,
    *,
    content: str,
    source_type: str,
    filename: str,
    file_url: str,
    upload_extra: Dict[str, Any],
) -> Dict[str, Any]:
    production = await _locked_production(db, production_id, user.id)
    if production.mode != "supervised":
        raise AppException("自动模式不支持人工补充剧本", code=40982, status_code=409)
    if production.status in {"draft", "paused", "cancelled"}:
        raise AppException("当前整剧状态不允许补充剧本", code=40983, status_code=409)

    step = await _latest_source_step(db, production.id)
    checkpoint = await _script_checkpoint(db, step.id)
    bible = await _latest_bible(db, production.id)
    pending_review = (
        step.status == "waiting_approval"
        and checkpoint is not None
        and checkpoint.status == "pending"
        and bible is not None
        and bible.status == "draft"
    )
    confirmed_baseline = (
        step.status == "completed"
        and checkpoint is not None
        and checkpoint.status == "approved"
        and bible is not None
        and bible.status == "confirmed"
    )
    if not pending_review and not confirmed_baseline:
        raise AppException("当前剧本处理结果已确认或不可补充", code=40983, status_code=409)

    source = await db.get(ProjectSourceDocument, production.source_document_id)
    if source is None:
        raise AppException("整剧原文不存在", code=40431, status_code=404)
    supplement = content.strip()
    if not supplement:
        raise AppException("补充剧本内容不能为空", code=40049, status_code=400)
    existing_content = source.content.rstrip()
    incremental_source_start = len(existing_content)
    combined = f"{existing_content}\n\n{supplement}"
    if settings.agent_source_max_characters > 0 and len(combined) > settings.agent_source_max_characters:
        raise AppException(
            f"补充后的整剧剧本不能超过 {settings.agent_source_max_characters} 个字符",
            code=40050,
            status_code=400,
        )

    new_source = await _save_source_document(
        db,
        project_id=production.project_id,
        user_id=user.id,
        source_type="text",
        filename=filename,
        content=combined,
        file_url=file_url,
        upload_extra={
            **upload_extra,
            "supplement_source_type": source_type,
            "previous_source_document_id": str(source.id),
            "appended_character_count": len(supplement),
            "incremental_source_start": incremental_source_start,
            "incremental_source_end": len(combined),
        },
        commit=False,
    )
    if pending_review:
        step.status = transition_step_status(step.status, "invalidated")
        checkpoint.status = "rejected"
        checkpoint.summary = "用户补充了新剧本，原分析结果已失效。"
        checkpoint.extra = {
            **(checkpoint.extra or {}),
            "invalidated_by_source_document_id": str(new_source.id),
        }
    new_step = AgentStep(
        production_id=production.id,
        stage="source_analysis",
        scope_type="production",
        scope_id=production.id,
        status="queued",
        input_version=new_source.version,
        output_version=int(step.output_version or 1),
        progress_current=0,
        progress_total=1,
        attempt_count=0,
        extra={
            "queued_by_user_id": str(user.id),
            "incremental": True,
            "analysis_scope": "incremental" if confirmed_baseline else "full",
            **(
                {
                    "incremental_source_start": incremental_source_start,
                    "incremental_source_end": len(combined),
                }
                if confirmed_baseline
                else {}
            ),
            "previous_source_document_id": str(source.id),
            "previous_source_step_id": str(step.id),
            "previous_bible_version_id": str(bible.id),
            "chunks": list((step.extra or {}).get("chunks") or []),
        },
    )
    db.add(new_step)
    await db.flush()
    production.source_document_id = new_source.id
    if production.status in {"waiting_approval", "completed"}:
        production.status = transition_production_status(production.status, "planning")
    elif production.status == "partially_failed":
        production.status = transition_production_status(production.status, "running")
    production.current_stage = "source_analysis"
    production.lock_version += 1
    production.updated_at = beijing_datetime()
    db.add(
        AgentEvent(
            production_id=production.id,
            step_id=new_step.id,
            actor_user_id=user.id,
            event_type="script.supplemented",
            source="user",
            payload={
                "previous_source_document_id": str(source.id),
                "source_document_id": str(new_source.id),
                "source_version": new_source.version,
                "appended_character_count": len(supplement),
            },
        )
    )
    await db.commit()
    await _enqueue_source_analysis(db, production.id, new_step.id)
    refreshed = await get_agent_production_or_404(db, production.id, user.id)
    return {
        "production_id": production.id,
        "source_document_id": new_source.id,
        "source_version": new_source.version,
        "step_id": new_step.id,
        "appended_character_count": len(supplement),
        "status": refreshed.status,
        "current_stage": refreshed.current_stage,
    }


async def _locked_production(
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


async def _latest_source_step(db: AsyncSession, production_id: UUID) -> AgentStep:
    result = await db.execute(
        select(AgentStep)
        .where(
            AgentStep.production_id == production_id,
            AgentStep.stage == "source_analysis",
            AgentStep.scope_type == "production",
        )
        .order_by(AgentStep.input_version.desc(), AgentStep.created_at.desc())
        .with_for_update(of=AgentStep)
        .limit(1)
    )
    step = result.scalar_one_or_none()
    if step is None:
        raise AppException("剧本处理步骤不存在", code=40983, status_code=409)
    return step


async def _script_checkpoint(
    db: AsyncSession,
    step_id: UUID,
) -> Optional[AgentCheckpoint]:
    result = await db.execute(
        select(AgentCheckpoint)
        .where(
            AgentCheckpoint.step_id == step_id,
            AgentCheckpoint.checkpoint_type == "script_review",
        )
        .with_for_update(of=AgentCheckpoint)
    )
    return result.scalar_one_or_none()


async def _latest_bible(
    db: AsyncSession,
    production_id: UUID,
) -> Optional[SeriesBibleVersion]:
    result = await db.execute(
        select(SeriesBibleVersion)
        .where(SeriesBibleVersion.production_id == production_id)
        .order_by(SeriesBibleVersion.version.desc())
        .with_for_update(of=SeriesBibleVersion)
        .limit(1)
    )
    return result.scalar_one_or_none()
