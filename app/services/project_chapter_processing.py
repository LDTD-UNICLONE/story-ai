import logging
from typing import Optional, Tuple
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException
from app.models.ai_model import AiModel
from app.models.project_chapter import ProjectChapter
from app.models.task_record import UserTaskRecord
from app.models.user import User
from app.schemas.project_chapter import ProjectChapterProcessRequest
from app.services.model_points import calculate_text_submission_points_cost
from app.services.points import change_user_points, consume_user_points
from app.services.project_chapters import get_project_chapter_or_404
from app.services.task_records import create_user_task_record
from app.services.text_model_extra import normalize_text_analysis_extra

logger = logging.getLogger(__name__)


async def submit_project_chapter_processing(
    db: AsyncSession,
    project_id: UUID,
    chapter_id: UUID,
    user: User,
    payload: ProjectChapterProcessRequest,
) -> Tuple[ProjectChapter, UserTaskRecord, int]:
    chapter = await get_project_chapter_or_404(db, project_id, chapter_id, user.id)
    if not (chapter.content or "").strip():
        raise AppException("章节原文内容不能为空", code=40036, status_code=400)
    ai_model = await get_enabled_text_model_or_404(db, payload.ai_model_id)
    points_cost = calculate_text_submission_points_cost(ai_model)
    points_transaction = None
    if points_cost > 0:
        points_transaction = await consume_user_points(
            db,
            user_id=user.id,
            amount=points_cost,
            remark=f"项目章节处理：{chapter.title}",
            auto_commit=False,
        )

    custom_system_prompt = (payload.processing_prompt or "").strip()
    prompt_source = "custom" if custom_system_prompt else "system"
    model_extra = normalize_text_analysis_extra(payload.extra)
    if custom_system_prompt:
        model_extra["system_prompt"] = custom_system_prompt
    task_prompt = _build_chapter_model_prompt(chapter.content, prompt_source)

    chapter.ai_model_id = ai_model.id
    chapter.processing_prompt = custom_system_prompt or None
    chapter.process_status = "pending"
    chapter.extra = {
        **(chapter.extra or {}),
        "process_extra": model_extra,
        "prompt_source": prompt_source,
    }
    task_record = await create_user_task_record(
        db,
        user_id=user.id,
        ai_model_id=ai_model.id,
        points_transaction_id=points_transaction.id if points_transaction else None,
        business_type="project",
        business_id=project_id,
        generation_type="chapter_text_process",
        status="pending",
        title=f"章节处理：{chapter.title}",
        prompt=task_prompt,
        result=None,
        points_cost=points_cost,
        extra={
            "project_id": str(project_id),
            "chapter_id": str(chapter.id),
            "chapter_title": chapter.title,
            "prompt_source": prompt_source,
            "model_extra": model_extra,
        },
    )
    if custom_system_prompt:
        task_record.extra = {
            **(task_record.extra or {}),
            "processing_prompt": custom_system_prompt,
        }
    await db.flush()
    chapter.extra = {
        **(chapter.extra or {}),
        "task_record_id": str(task_record.id),
    }
    await db.commit()
    await db.refresh(chapter)

    try:
        from app.tasks.project_chapter import run_project_chapter_processing

        run_project_chapter_processing.apply_async(
            args=(str(task_record.id), str(chapter.id)),
            queue="story_ai_text",
            routing_key="story_ai_text",
        )
    except Exception as exc:
        logger.exception("Project chapter task enqueue failed: task_record_id=%s", task_record.id)
        await _mark_chapter_enqueue_failed(db, task_record, chapter, exc)
        await db.refresh(chapter)
    return chapter, task_record, points_cost


async def get_enabled_text_model_or_404(db: AsyncSession, ai_model_id: UUID) -> AiModel:
    result = await db.execute(
        select(AiModel).where(
            AiModel.id == ai_model_id,
            AiModel.model_type == "text",
            AiModel.is_enabled.is_(True),
        )
    )
    ai_model = result.scalar_one_or_none()
    if ai_model is None:
        raise AppException("文本模型不存在、未启用或类型不匹配", code=40404, status_code=404)
    return ai_model


def _build_chapter_model_prompt(chapter_content: str, prompt_source: str) -> str:
    content = chapter_content.strip()
    if prompt_source == "custom":
        return f"请根据系统规则处理以下原文内容：\n\n{content}"
    return "系统提示词"


async def _mark_chapter_enqueue_failed(
    db: AsyncSession,
    task_record: UserTaskRecord,
    chapter: ProjectChapter,
    exc: Optional[Exception] = None,
) -> None:
    refund_transaction_id = None
    if task_record.points_cost > 0:
        refund_transaction = await change_user_points(
            db,
            user_id=task_record.user_id,
            amount=task_record.points_cost,
            transaction_type="refund",
            remark=f"任务入队失败退回积分：{task_record.title}",
            auto_commit=False,
        )
        refund_transaction_id = str(refund_transaction.id)
    task_record.status = "failed"
    task_record.result = "任务入队失败"
    task_record.extra = {
        **(task_record.extra or {}),
        "failed_reason": "任务入队失败",
        "raw_failed_reason": str(exc) if exc else "任务入队失败",
        "refund_transaction_id": refund_transaction_id,
    }
    chapter.process_status = "failed"
    chapter.extra = {
        **(chapter.extra or {}),
        "failed_reason": "任务入队失败",
    }
    await db.commit()
