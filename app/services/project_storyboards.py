import json
import logging
import re
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException
from app.core.logging import log_extra
from app.core.timezone import beijing_datetime
from app.models.ai_model import AiModel
from app.models.project_asset import ProjectCharacter, ProjectProp, ProjectScene
from app.models.project_chapter import ProjectChapter
from app.models.project_storyboard import ProjectStoryboard
from app.models.task_record import UserTaskRecord
from app.models.user import User
from app.schemas.project_storyboard import (
    ProjectStoryboardAnalyzeRequest,
    ProjectStoryboardMergeRequest,
    ProjectStoryboardPromptRequest,
    ProjectStoryboardRefineRequest,
    ProjectStoryboardSplitRequest,
    ProjectStoryboardUpdateRequest,
)
from app.services.model_points import calculate_text_submission_points_cost, settle_text_task_points
from app.services.model_runner import run_model
from app.services.points import change_user_points, consume_user_points
from app.services.project_chapter_processing import get_enabled_text_model_or_404
from app.services.project_chapters import get_project_chapter_or_404
from app.services.prompts import render_system_prompt
from app.services.projects import get_project_or_404
from app.services.task_records import (
    create_user_task_record,
    expire_stale_task_record,
    refresh_task_record_interrupted,
)
from app.services.text_model_extra import normalize_text_analysis_extra


logger = logging.getLogger(__name__)


async def list_project_storyboards(
    db: AsyncSession,
    project_id: UUID,
    chapter_id: UUID,
    user_id: UUID,
    page: int,
    page_size: int,
) -> Tuple[List[ProjectStoryboard], int]:
    await get_project_chapter_or_404(db, project_id, chapter_id, user_id)
    conditions = [
        ProjectStoryboard.project_id == project_id,
        ProjectStoryboard.chapter_id == chapter_id,
        ProjectStoryboard.user_id == user_id,
        ProjectStoryboard.is_enabled.is_(True),
    ]
    count_result = await db.execute(select(func.count()).select_from(ProjectStoryboard).where(*conditions))
    total = count_result.scalar_one()
    result = await db.execute(
        select(ProjectStoryboard)
        .where(*conditions)
        .order_by(ProjectStoryboard.shot_number.asc(), ProjectStoryboard.created_at.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(result.scalars().all()), total


async def get_project_storyboard_or_404(
    db: AsyncSession,
    project_id: UUID,
    chapter_id: UUID,
    storyboard_id: UUID,
    user_id: UUID,
) -> ProjectStoryboard:
    result = await db.execute(
        select(ProjectStoryboard).where(
            ProjectStoryboard.id == storyboard_id,
            ProjectStoryboard.project_id == project_id,
            ProjectStoryboard.chapter_id == chapter_id,
            ProjectStoryboard.user_id == user_id,
            ProjectStoryboard.is_enabled.is_(True),
        )
    )
    storyboard = result.scalar_one_or_none()
    if storyboard is None:
        raise AppException("项目分镜不存在", code=40410, status_code=404)
    await reconcile_storyboard_video_task(db, storyboard)
    return storyboard


async def reconcile_storyboard_video_task(db: AsyncSession, storyboard: ProjectStoryboard) -> None:
    task_record_id = (storyboard.extra or {}).get("video_generation_task_record_id")
    status = (storyboard.extra or {}).get("video_generation_status")
    if not task_record_id or status not in {"pending", "running"}:
        return
    parsed_task_record_id = _parse_uuid(task_record_id)
    if parsed_task_record_id is None:
        return
    task_record = await db.get(UserTaskRecord, parsed_task_record_id)
    if task_record is None:
        return
    await expire_stale_task_record(db, task_record)
    await db.refresh(storyboard)


def _parse_uuid(value: Any) -> Optional[UUID]:
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


async def update_project_storyboard(
    db: AsyncSession,
    project_id: UUID,
    chapter_id: UUID,
    storyboard_id: UUID,
    user_id: UUID,
    payload: ProjectStoryboardUpdateRequest,
) -> ProjectStoryboard:
    storyboard = await get_project_storyboard_or_404(db, project_id, chapter_id, storyboard_id, user_id)
    update_data = payload.model_dump(exclude_unset=True)
    core_action = update_data.pop("core_action", None)
    storyboard_image_prompt = update_data.pop("storyboard_image_prompt", None)
    event_goal = update_data.pop("event_goal", None)
    production_focus_base = update_data.pop("production_focus_base", None)
    negative_prompt_base = update_data.pop("negative_prompt_base", None)
    split_reason = update_data.pop("split_reason", None)
    if core_action is not None:
        update_data["action"] = core_action
    if storyboard_image_prompt is not None:
        update_data["image_prompt"] = storyboard_image_prompt
    if production_focus_base is not None and "production_focus" not in update_data:
        update_data["production_focus"] = production_focus_base
    if negative_prompt_base is not None and "negative_prompt" not in update_data:
        update_data["negative_prompt"] = negative_prompt_base
    for field, value in update_data.items():
        setattr(storyboard, field, value)
    if event_goal is not None or split_reason is not None:
        storyboard.extra = {
            **(storyboard.extra or {}),
            **({"event_goal": event_goal} if event_goal is not None else {}),
            **({"split_reason": split_reason} if split_reason is not None else {}),
        }
    storyboard.updated_at = beijing_datetime()
    await db.commit()
    await db.refresh(storyboard)
    return storyboard


async def delete_project_storyboard(
    db: AsyncSession,
    project_id: UUID,
    chapter_id: UUID,
    storyboard_id: UUID,
    user_id: UUID,
) -> ProjectStoryboard:
    storyboard = await get_project_storyboard_or_404(db, project_id, chapter_id, storyboard_id, user_id)
    storyboard.is_enabled = False
    storyboard.updated_at = beijing_datetime()
    await db.commit()
    await db.refresh(storyboard)
    return storyboard


async def merge_project_storyboards(
    db: AsyncSession,
    project_id: UUID,
    chapter_id: UUID,
    user_id: UUID,
    payload: ProjectStoryboardMergeRequest,
) -> List[ProjectStoryboard]:
    await get_project_chapter_or_404(db, project_id, chapter_id, user_id)
    storyboard_ids = _unique_uuids(payload.storyboard_ids)
    if len(storyboard_ids) < 2:
        raise AppException("至少选择两个分镜进行合并", code=40031, status_code=400)

    storyboards = await _list_enabled_storyboards(db, project_id, chapter_id, user_id)
    selected_ids = set(storyboard_ids)
    selected_map = {storyboard.id: storyboard for storyboard in storyboards if storyboard.id in selected_ids}
    if len(selected_map) != len(storyboard_ids):
        raise AppException("待合并分镜不存在或已不可用", code=40410, status_code=404)

    selected = [storyboard for storyboard in storyboards if storyboard.id in selected_map]
    first_selected_index = next(index for index, storyboard in enumerate(storyboards) if storyboard.id in selected_map)
    merged_item = _build_merged_storyboard_item(selected, payload)

    now = beijing_datetime()
    for storyboard in selected:
        storyboard.is_enabled = False
        storyboard.updated_at = now

    merged_storyboard = _make_storyboard_from_item(
        project_id=project_id,
        chapter_id=chapter_id,
        user_id=user_id,
        ai_model_id=selected[0].ai_model_id,
        item=merged_item,
        index=first_selected_index + 1,
        extra={
            "operation": "merge",
            "merged_storyboard_ids": [str(storyboard.id) for storyboard in selected],
        },
    )
    db.add(merged_storyboard)
    await db.flush()

    final_order: List[ProjectStoryboard] = []
    merged_inserted = False
    for index, storyboard in enumerate(storyboards):
        if index == first_selected_index:
            final_order.append(merged_storyboard)
            merged_inserted = True
        if storyboard.id not in selected_map:
            final_order.append(storyboard)
    if not merged_inserted:
        final_order.append(merged_storyboard)

    _assign_shot_numbers(final_order)
    await db.commit()
    return await _list_enabled_storyboards(db, project_id, chapter_id, user_id)


async def split_project_storyboard(
    db: AsyncSession,
    project_id: UUID,
    chapter_id: UUID,
    storyboard_id: UUID,
    user_id: UUID,
    payload: ProjectStoryboardSplitRequest,
) -> List[ProjectStoryboard]:
    storyboard = await get_project_storyboard_or_404(db, project_id, chapter_id, storyboard_id, user_id)
    storyboards = await _list_enabled_storyboards(db, project_id, chapter_id, user_id)
    original_index = next((index for index, item in enumerate(storyboards) if item.id == storyboard.id), None)
    if original_index is None:
        raise AppException("项目分镜不存在", code=40410, status_code=404)

    now = beijing_datetime()
    storyboard.is_enabled = False
    storyboard.updated_at = now

    new_storyboards: List[ProjectStoryboard] = []
    for offset, unit in enumerate(payload.units, start=1):
        item = _normalize_storyboard_item(unit.model_dump(exclude_none=True), offset)
        new_storyboard = _make_storyboard_from_item(
            project_id=project_id,
            chapter_id=chapter_id,
            user_id=user_id,
            ai_model_id=storyboard.ai_model_id,
            item=item,
            index=storyboard.shot_number + offset - 1,
            extra={
                "operation": "split",
                "split_from_storyboard_id": str(storyboard.id),
            },
        )
        db.add(new_storyboard)
        new_storyboards.append(new_storyboard)
    await db.flush()

    final_order = storyboards[:original_index] + new_storyboards + storyboards[original_index + 1 :]
    _assign_shot_numbers(final_order)
    await db.commit()
    return await _list_enabled_storyboards(db, project_id, chapter_id, user_id)


async def submit_storyboard_refinement(
    db: AsyncSession,
    project_id: UUID,
    chapter_id: UUID,
    storyboard_id: UUID,
    user: User,
    payload: ProjectStoryboardRefineRequest,
) -> Tuple[UserTaskRecord, int]:
    chapter = await get_project_chapter_or_404(db, project_id, chapter_id, user.id)
    storyboard = await get_project_storyboard_or_404(
        db,
        project_id=project_id,
        chapter_id=chapter_id,
        storyboard_id=storyboard_id,
        user_id=user.id,
    )

    prompt = render_system_prompt(
        "storyboard_refinement.md",
        storyboard_unit=json.dumps(_storyboard_unit_payload(storyboard), ensure_ascii=False),
        characters=await _dump_storyboard_assets(db, ProjectCharacter, project_id, user.id),
        scenes=await _dump_storyboard_assets(db, ProjectScene, project_id, user.id),
        props=await _dump_storyboard_assets(db, ProjectProp, project_id, user.id),
    )
    return await _submit_storyboard_text_stage(
        db,
        project_id=project_id,
        chapter=chapter,
        user=user,
        ai_model_id=payload.ai_model_id,
        prompt=prompt,
        extra=payload.extra,
        storyboard=storyboard,
        generation_type="storyboard_refinement",
        title_prefix="分镜细化与视频提示词生成",
        status_key="storyboard_refinement_status",
        task_key="storyboard_refinement_task_record_id",
    )


async def submit_storyboard_image_prompt_generation(
    db: AsyncSession,
    project_id: UUID,
    chapter_id: UUID,
    storyboard_id: UUID,
    user: User,
    payload: ProjectStoryboardPromptRequest,
) -> Tuple[UserTaskRecord, int]:
    chapter = await get_project_chapter_or_404(db, project_id, chapter_id, user.id)
    storyboard = await get_project_storyboard_or_404(
        db,
        project_id=project_id,
        chapter_id=chapter_id,
        storyboard_id=storyboard_id,
        user_id=user.id,
    )
    if not storyboard.visual_description:
        raise AppException("请先完成分镜细化或填写画面描述，再生成故事板提示词", code=40032, status_code=400)

    prompt = render_system_prompt(
        "storyboard_image_prompt_generation.md",
        storyboard_execution_item=json.dumps(_storyboard_execution_payload(storyboard), ensure_ascii=False),
        characters=await _dump_storyboard_assets(db, ProjectCharacter, project_id, user.id),
        scenes=await _dump_storyboard_assets(db, ProjectScene, project_id, user.id),
        props=await _dump_storyboard_assets(db, ProjectProp, project_id, user.id),
    )
    return await _submit_storyboard_text_stage(
        db,
        project_id=project_id,
        chapter=chapter,
        user=user,
        ai_model_id=payload.ai_model_id,
        prompt=prompt,
        extra=payload.extra,
        storyboard=storyboard,
        generation_type="storyboard_image_prompt_generation",
        title_prefix="故事板提示词生成",
        status_key="storyboard_image_prompt_generation_status",
        task_key="storyboard_image_prompt_generation_task_record_id",
    )


async def submit_storyboard_analysis(
    db: AsyncSession,
    project_id: UUID,
    chapter_id: UUID,
    user: User,
    payload: ProjectStoryboardAnalyzeRequest,
) -> Tuple[UserTaskRecord, int]:
    await get_project_or_404(db, project_id, user.id)
    chapter = await get_project_chapter_or_404(db, project_id, chapter_id, user.id)
    if not chapter.processed_content:
        raise AppException("章节还没有处理后的内容，无法分析分镜", code=40011, status_code=400)

    ai_model = await get_enabled_text_model_or_404(db, payload.ai_model_id)
    points_cost = calculate_text_submission_points_cost(ai_model)
    points_transaction = None
    if points_cost > 0:
        points_transaction = await consume_user_points(
            db,
            user_id=user.id,
            amount=points_cost,
            remark=f"项目章节分镜分析：{chapter.title}",
            auto_commit=False,
        )

    model_extra = normalize_text_analysis_extra(payload.extra)
    prompt = payload.analysis_prompt or render_system_prompt(
        "storyboard_analysis.md",
        input_text=chapter.processed_content,
        characters=await _dump_storyboard_assets(db, ProjectCharacter, project_id, user.id),
        scenes=await _dump_storyboard_assets(db, ProjectScene, project_id, user.id),
        props=await _dump_storyboard_assets(db, ProjectProp, project_id, user.id),
    )
    task_record = await create_user_task_record(
        db,
        user_id=user.id,
        ai_model_id=ai_model.id,
        points_transaction_id=points_transaction.id if points_transaction else None,
        business_type="project",
        business_id=project_id,
        generation_type="storyboard_analysis",
        status="pending",
        title=f"分镜分析：{chapter.title}",
        prompt=prompt,
        result=None,
        points_cost=points_cost,
        extra={
            "project_id": str(project_id),
            "chapter_id": str(chapter_id),
            "chapter_title": chapter.title,
            "model_extra": model_extra,
        },
    )
    chapter.extra = {
        **(chapter.extra or {}),
        "storyboard_analysis_status": "pending",
        "storyboard_analysis_task_record_id": str(task_record.id),
    }
    await db.commit()

    try:
        from app.tasks.project_storyboard import run_project_storyboard_analysis

        run_project_storyboard_analysis.delay(str(task_record.id), str(chapter_id))
    except Exception:
        await _mark_storyboard_enqueue_failed(db, task_record, chapter)
    return task_record, points_cost


async def _submit_storyboard_text_stage(
    db: AsyncSession,
    *,
    project_id: UUID,
    chapter: ProjectChapter,
    user: User,
    ai_model_id: UUID,
    prompt: str,
    extra: Optional[Dict[str, Any]],
    storyboard: Optional[ProjectStoryboard],
    generation_type: str,
    title_prefix: str,
    status_key: str,
    task_key: str,
) -> Tuple[UserTaskRecord, int]:
    ai_model = await get_enabled_text_model_or_404(db, ai_model_id)
    points_cost = calculate_text_submission_points_cost(ai_model)
    points_transaction = None
    if points_cost > 0:
        points_transaction = await consume_user_points(
            db,
            user_id=user.id,
            amount=points_cost,
            remark=_storyboard_stage_points_remark(title_prefix, chapter, storyboard),
            auto_commit=False,
        )

    model_extra = normalize_text_analysis_extra(extra)
    task_title = _storyboard_stage_task_title(title_prefix, chapter, storyboard)
    task_record = await create_user_task_record(
        db,
        user_id=user.id,
        ai_model_id=ai_model.id,
        points_transaction_id=points_transaction.id if points_transaction else None,
        business_type="project",
        business_id=project_id,
        generation_type=generation_type,
        status="pending",
        title=task_title,
        prompt=prompt,
        result=None,
        points_cost=points_cost,
        extra={
            "project_id": str(project_id),
            "chapter_id": str(chapter.id),
            "chapter_title": chapter.title,
            **_storyboard_stage_extra(storyboard),
            "model_extra": model_extra,
        },
    )
    chapter.extra = {
        **(chapter.extra or {}),
        status_key: "pending",
        task_key: str(task_record.id),
    }
    if storyboard is not None:
        storyboard.extra = {
            **(storyboard.extra or {}),
            status_key: "pending",
            task_key: str(task_record.id),
        }
    await db.commit()

    try:
        from app.tasks.project_storyboard import run_project_storyboard_stage

        run_project_storyboard_stage.delay(str(task_record.id), str(chapter.id))
    except Exception:
        await _mark_storyboard_stage_enqueue_failed(db, task_record, chapter, status_key, storyboard)
    return task_record, points_cost


async def run_storyboard_analysis_in_worker(
    db: AsyncSession,
    task_record: UserTaskRecord,
    chapter: ProjectChapter,
) -> None:
    result = await db.execute(
        select(AiModel).where(
            AiModel.id == task_record.ai_model_id,
            AiModel.model_type == "text",
            AiModel.is_enabled.is_(True),
        )
    )
    ai_model = result.scalar_one_or_none()
    if ai_model is None:
        raise AppException("文本模型不存在或已禁用", code=40404, status_code=404)

    model_snapshot = SimpleNamespace(
        id=ai_model.id,
        model_id=ai_model.model_id,
        vendor=ai_model.vendor,
        nickname=ai_model.nickname,
        points_cost=ai_model.points_cost,
        capabilities=ai_model.capabilities or {},
    )
    model_result = await run_model(
        model_snapshot,
        "text",
        task_record.prompt,
        (task_record.extra or {}).get("model_extra") or {},
    )
    if await refresh_task_record_interrupted(db, task_record):
        return

    items = parse_storyboard_items(model_result.content)
    if not items:
        logger.warning(
            "Storyboard analysis model result is invalid",
            extra=log_extra(
                event="storyboard_analysis_invalid_result",
                task_record_id=task_record.id,
                chapter_id=chapter.id,
                result_preview=(model_result.content or "")[:500],
            ),
        )
        task_record.extra = {
            **(task_record.extra or {}),
            "invalid_model_result_preview": (model_result.content or "")[:2000],
            "model_result_extra": model_result.extra,
        }
        raise AppException("分镜分析未返回有效数据", code=50231, status_code=502)

    logger.info(
        "Storyboard analysis model result parsed",
        extra=log_extra(
            event="storyboard_analysis_parsed",
            task_record_id=task_record.id,
            chapter_id=chapter.id,
            storyboard_count=len(items),
        ),
    )

    await settle_text_task_points(
        db,
        task_record,
        ai_model,
        model_result.extra,
        remark_prefix="分镜分析",
    )

    await db.execute(
        update(ProjectStoryboard)
        .where(
            ProjectStoryboard.project_id == task_record.business_id,
            ProjectStoryboard.chapter_id == chapter.id,
            ProjectStoryboard.user_id == task_record.user_id,
            ProjectStoryboard.is_enabled.is_(True),
        )
        .values(is_enabled=False, updated_at=beijing_datetime())
    )
    for index, item in enumerate(items, start=1):
        storyboard = _make_storyboard_from_item(
            project_id=task_record.business_id,
            chapter_id=chapter.id,
            user_id=task_record.user_id,
            ai_model_id=task_record.ai_model_id,
            item=item,
            index=index,
            extra={"task_record_id": str(task_record.id)},
        )
        db.add(storyboard)

    chapter.extra = {
        **_clear_status_retry_state(chapter.extra or {}, "storyboard_analysis_status"),
        "storyboard_analysis_status": "success",
        "storyboard_analysis_task_record_id": str(task_record.id),
    }
    task_record.status = "success"
    task_record.result = model_result.content
    task_record.extra = {
        **_clear_task_retry_state(task_record.extra or {}),
        "model_result_extra": model_result.extra,
        "storyboard_count": len(items),
    }
    logger.info(
        "Storyboard analysis applied to database session",
        extra=log_extra(
            event="storyboard_analysis_applied",
            task_record_id=task_record.id,
            chapter_id=chapter.id,
            storyboard_count=len(items),
        ),
    )


async def run_storyboard_stage_in_worker(
    db: AsyncSession,
    task_record: UserTaskRecord,
    chapter: ProjectChapter,
) -> None:
    if task_record.generation_type == "storyboard_analysis":
        await run_storyboard_analysis_in_worker(db, task_record, chapter)
        return

    result = await db.execute(
        select(AiModel).where(
            AiModel.id == task_record.ai_model_id,
            AiModel.model_type == "text",
            AiModel.is_enabled.is_(True),
        )
    )
    ai_model = result.scalar_one_or_none()
    if ai_model is None:
        raise AppException("文本模型不存在或已禁用", code=40404, status_code=404)

    model_snapshot = SimpleNamespace(
        id=ai_model.id,
        model_id=ai_model.model_id,
        vendor=ai_model.vendor,
        nickname=ai_model.nickname,
        points_cost=ai_model.points_cost,
        capabilities=ai_model.capabilities or {},
    )
    model_result = await run_model(
        model_snapshot,
        "text",
        task_record.prompt,
        (task_record.extra or {}).get("model_extra") or {},
    )
    if await refresh_task_record_interrupted(db, task_record):
        return

    items = parse_storyboard_stage_items(model_result.content, task_record.generation_type)
    if not items:
        logger.warning(
            "Storyboard stage model result is invalid",
            extra=log_extra(
                event="storyboard_stage_invalid_result",
                task_record_id=task_record.id,
                chapter_id=chapter.id,
                generation_type=task_record.generation_type,
                result_preview=(model_result.content or "")[:500],
            ),
        )
        task_record.extra = {
            **(task_record.extra or {}),
            "invalid_model_result_preview": (model_result.content or "")[:2000],
            "model_result_extra": model_result.extra,
        }
        raise AppException("分镜阶段任务未返回有效数据", code=50231, status_code=502)

    logger.info(
        "Storyboard stage model result parsed",
        extra=log_extra(
            event="storyboard_stage_parsed",
            task_record_id=task_record.id,
            chapter_id=chapter.id,
            generation_type=task_record.generation_type,
            item_count=len(items),
        ),
    )

    await settle_text_task_points(
        db,
        task_record,
        ai_model,
        model_result.extra,
        remark_prefix=_storyboard_stage_title(task_record.generation_type),
    )

    if task_record.generation_type == "storyboard_refinement":
        await _apply_storyboard_refinement_items(db, task_record, chapter, items)
    elif task_record.generation_type == "storyboard_image_prompt_generation":
        await _apply_storyboard_image_prompt_items(db, task_record, chapter, items)
    elif task_record.generation_type == "storyboard_prompt_generation":
        await _apply_storyboard_prompt_items(db, task_record, chapter, items)
    else:
        raise AppException("不支持的分镜阶段任务", code=40033, status_code=400)

    status_key, task_key = _storyboard_stage_keys(task_record.generation_type)
    chapter.extra = {
        **_clear_status_retry_state(chapter.extra or {}, status_key),
        status_key: "success",
        task_key: str(task_record.id),
    }
    task_record.status = "success"
    task_record.result = model_result.content
    task_record.extra = {
        **_clear_task_retry_state(task_record.extra or {}),
        "model_result_extra": model_result.extra,
        "storyboard_count": len(items),
    }
    logger.info(
        "Storyboard stage applied to database session",
        extra=log_extra(
            event="storyboard_stage_applied",
            task_record_id=task_record.id,
            chapter_id=chapter.id,
            generation_type=task_record.generation_type,
            item_count=len(items),
        ),
    )


async def _apply_storyboard_refinement_items(
    db: AsyncSession,
    task_record: UserTaskRecord,
    chapter: ProjectChapter,
    items: List[Dict[str, Any]],
) -> None:
    storyboard_id = _task_storyboard_id(task_record)
    if storyboard_id is not None:
        if len(items) != 1:
            raise AppException("单个分镜细化结果必须只包含一条分镜", code=50231, status_code=502)
        storyboard = await get_project_storyboard_or_404(
            db,
            project_id=task_record.business_id,
            chapter_id=chapter.id,
            storyboard_id=storyboard_id,
            user_id=task_record.user_id,
        )
        _apply_storyboard_refinement_item(storyboard, task_record, items[0], beijing_datetime())
        return

    storyboards = await _list_enabled_storyboards(db, task_record.business_id, chapter.id, task_record.user_id)
    if len(items) != len(storyboards):
        raise AppException("分镜细化结果必须与当前分镜数量一致", code=50231, status_code=502)
    by_shot_number = {storyboard.shot_number: storyboard for storyboard in storyboards}
    seen_shot_numbers: set[int] = set()
    now = beijing_datetime()
    for item in items:
        shot_number = _as_int(item.get("shot_number"), 0)
        if shot_number in seen_shot_numbers:
            raise AppException("分镜细化结果包含重复分镜编号", code=50231, status_code=502)
        seen_shot_numbers.add(shot_number)
        storyboard = by_shot_number.get(shot_number)
        if storyboard is None:
            raise AppException("分镜细化结果包含不存在的分镜编号", code=50231, status_code=502)
        _apply_storyboard_refinement_item(storyboard, task_record, item, now)


async def _apply_storyboard_prompt_items(
    db: AsyncSession,
    task_record: UserTaskRecord,
    chapter: ProjectChapter,
    items: List[Dict[str, Any]],
) -> None:
    storyboard_id = _task_storyboard_id(task_record)
    if storyboard_id is not None:
        if len(items) != 1:
            raise AppException("单个分镜提示词结果必须只包含一条分镜", code=50231, status_code=502)
        storyboard = await get_project_storyboard_or_404(
            db,
            project_id=task_record.business_id,
            chapter_id=chapter.id,
            storyboard_id=storyboard_id,
            user_id=task_record.user_id,
        )
        _apply_storyboard_prompt_item(storyboard, task_record, items[0], beijing_datetime())
        return

    storyboards = await _list_enabled_storyboards(db, task_record.business_id, chapter.id, task_record.user_id)
    if len(items) != len(storyboards):
        raise AppException("视频提示词结果必须与当前分镜数量一致", code=50231, status_code=502)
    by_shot_number = {storyboard.shot_number: storyboard for storyboard in storyboards}
    seen_shot_numbers: set[int] = set()
    now = beijing_datetime()
    for item in items:
        shot_number = _as_int(item.get("shot_number"), 0)
        if shot_number in seen_shot_numbers:
            raise AppException("视频提示词结果包含重复分镜编号", code=50231, status_code=502)
        seen_shot_numbers.add(shot_number)
        storyboard = by_shot_number.get(shot_number)
        if storyboard is None:
            raise AppException("视频提示词结果包含不存在的分镜编号", code=50231, status_code=502)
        _apply_storyboard_prompt_item(storyboard, task_record, item, now)


async def _apply_storyboard_image_prompt_items(
    db: AsyncSession,
    task_record: UserTaskRecord,
    chapter: ProjectChapter,
    items: List[Dict[str, Any]],
) -> None:
    storyboard_id = _task_storyboard_id(task_record)
    if storyboard_id is not None:
        if len(items) != 1:
            raise AppException("单个故事板提示词结果必须只包含一条分镜", code=50231, status_code=502)
        storyboard = await get_project_storyboard_or_404(
            db,
            project_id=task_record.business_id,
            chapter_id=chapter.id,
            storyboard_id=storyboard_id,
            user_id=task_record.user_id,
        )
        _apply_storyboard_image_prompt_item(storyboard, task_record, items[0], beijing_datetime())
        return

    raise AppException("故事板提示词生成必须指定单个分镜", code=50231, status_code=502)


def _apply_storyboard_refinement_item(
    storyboard: ProjectStoryboard,
    task_record: UserTaskRecord,
    item: Dict[str, Any],
    now,
) -> None:
    storyboard.title = str(item.get("title") or storyboard.title)[:128]
    storyboard.source_content = str(item.get("source_content") or storyboard.source_content)
    storyboard.scene_name = _optional_str(item.get("scene_name"), 128)
    storyboard.scene_time = _optional_str(item.get("scene_time"), 64)
    storyboard.scene_state = _optional_str(_first_value(item, "scene_state", "场景状态"), 128)
    storyboard.characters = _as_string_list(item.get("characters"))
    storyboard.props = _as_string_list(item.get("props"))
    storyboard.action = _optional_str(item.get("action")) or storyboard.action
    storyboard.shot_size = _optional_str(item.get("shot_size"), 64)
    storyboard.camera_angle = _optional_str(item.get("camera_angle"), 128)
    storyboard.camera_movement = _optional_str(item.get("camera_movement"))
    storyboard.screen_execution = _optional_str(item.get("screen_execution"))
    storyboard.character_action = _optional_str(item.get("character_action"))
    storyboard.character_expression = _optional_str(item.get("character_expression"))
    storyboard.dialogue = _optional_str(item.get("dialogue"))
    storyboard.sound_effect = _optional_str(item.get("sound_effect"))
    storyboard.atmosphere = _optional_str(_first_value(item, "atmosphere", "氛围参考", "画面氛围"))
    storyboard.emotion = _optional_str(item.get("emotion"))
    storyboard.visual_description = _optional_str(item.get("visual_description"))
    storyboard.video_prompt = _optional_str(item.get("video_prompt"))
    storyboard.duration_suggestion = _optional_str(item.get("duration_suggestion"), 64)
    storyboard.production_focus = _optional_str(_first_value(item, "production_focus", "production_focus_base"))
    storyboard.negative_prompt = _optional_str(_first_value(item, "negative_prompt", "negative_prompt_base"))
    storyboard.ending_frame = _optional_str(_first_value(item, "ending_frame", "结尾画面", "收束画面"))
    storyboard.extra = {
        **_clear_status_retry_state(storyboard.extra or {}, "storyboard_refinement_status"),
        "storyboard_refinement_status": "success",
        "storyboard_refinement_task_record_id": str(task_record.id),
        "refinement_task_record_id": str(task_record.id),
        "refinement_raw_item": item,
    }
    storyboard.updated_at = now


def _apply_storyboard_image_prompt_item(
    storyboard: ProjectStoryboard,
    task_record: UserTaskRecord,
    item: Dict[str, Any],
    now,
) -> None:
    storyboard.image_prompt = _optional_str(
        _first_value(item, "storyboard_image_prompt", "image_prompt", "故事板提示词", "故事板图像提示词", "图像提示词")
    )
    storyboard.extra = {
        **_clear_status_retry_state(storyboard.extra or {}, "storyboard_image_prompt_generation_status"),
        "storyboard_image_prompt_generation_status": "success",
        "storyboard_image_prompt_generation_task_record_id": str(task_record.id),
        "storyboard_image_prompt_generation_raw_item": item,
    }
    storyboard.updated_at = now


def _apply_storyboard_prompt_item(
    storyboard: ProjectStoryboard,
    task_record: UserTaskRecord,
    item: Dict[str, Any],
    now,
) -> None:
    storyboard.image_prompt = _optional_str(
        _first_value(item, "storyboard_image_prompt", "image_prompt", "故事板图像提示词", "图像提示词")
    )
    storyboard.video_prompt = _optional_str(_first_value(item, "video_prompt", "视频提示词"))
    storyboard.extra = {
        **_clear_status_retry_state(storyboard.extra or {}, "storyboard_prompt_generation_status"),
        "storyboard_prompt_generation_status": "success",
        "storyboard_prompt_generation_task_record_id": str(task_record.id),
        "prompt_generation_task_record_id": str(task_record.id),
        "prompt_generation_raw_item": item,
    }
    storyboard.updated_at = now


def parse_storyboard_stage_items(content: str, generation_type: str) -> List[Dict[str, Any]]:
    payload = _parse_json_payload(content)
    if generation_type == "storyboard_refinement":
        items = _extract_items_by_keys(
            payload,
            ("storyboard_execution_item", "storyboard_execution_items", "item", "items", "分镜细化", "分镜细化列表"),
        )
        return [
            _normalize_storyboard_refinement_item(item, index)
            for index, item in enumerate(items or [], start=1)
            if isinstance(item, dict)
        ]
    if generation_type == "storyboard_image_prompt_generation":
        items = _extract_items_by_keys(
            payload,
            (
                "storyboard_image_prompt_item",
                "storyboard_image_prompt_items",
                "storyboard_prompt_item",
                "item",
                "items",
                "故事板提示词",
                "故事板提示词列表",
            ),
        )
        return [
            _normalize_storyboard_image_prompt_item(item, index)
            for index, item in enumerate(items or [], start=1)
            if isinstance(item, dict)
        ]
    if generation_type == "storyboard_prompt_generation":
        items = _extract_items_by_keys(
            payload,
            ("storyboard_prompt_item", "storyboard_prompt_items", "item", "items", "视频提示词", "视频提示词列表"),
        )
        return [
            _normalize_storyboard_prompt_item(item, index)
            for index, item in enumerate(items or [], start=1)
            if isinstance(item, dict)
        ]
    return parse_storyboard_items(content)


def _extract_items_by_keys(payload: Any, keys: Tuple[str, ...]) -> Optional[List[Any]]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return None
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict) and _looks_like_storyboard_item(value):
            return [value]
    return _extract_storyboard_items(payload)


def _normalize_storyboard_refinement_item(item: Dict[str, Any], index: int) -> Dict[str, Any]:
    return {
        **item,
        "shot_number": _first_value(item, "shot_number", "分镜序号") or index,
        "title": _first_value(item, "title", "标题") or f"分镜{index}",
        "source_content": _first_value(item, "source_content", "原文") or "",
        "scene_name": _first_value(item, "scene_name", "场景名称") or "",
        "scene_time": _first_value(item, "scene_time", "场景时间", "时间") or "",
        "scene_state": _first_value(item, "scene_state", "场景状态") or "",
        "characters": _first_value(item, "characters", "人物") or [],
        "props": _first_value(item, "props", "道具") or [],
        "action": _first_value(item, "action", "core_action", "核心动作", "动作") or "",
        "shot_size": _first_value(item, "shot_size", "景别") or "",
        "camera_angle": _first_value(item, "camera_angle", "拍摄角度") or "",
        "camera_movement": _first_value(item, "camera_movement", "运镜") or "",
        "screen_execution": _first_value(item, "screen_execution", "画面执行") or "",
        "character_action": _first_value(item, "character_action", "角色动作") or "",
        "character_expression": _first_value(item, "character_expression", "角色表情") or "",
        "dialogue": _first_value(item, "dialogue", "台词") or "",
        "sound_effect": _first_value(item, "sound_effect", "音效") or "",
        "atmosphere": _first_value(item, "atmosphere", "氛围参考", "画面氛围") or "",
        "emotion": _first_value(item, "emotion", "情绪") or "",
        "visual_description": _first_value(item, "visual_description", "画面内容") or "",
        "video_prompt": _first_value(item, "video_prompt", "视频提示词") or "",
        "duration_suggestion": _first_value(item, "duration_suggestion", "时长建议") or "",
        "production_focus": _first_value(item, "production_focus", "production_focus_base", "制作重点", "制作重点提示词", "制作重点基础") or "",
        "negative_prompt": _first_value(item, "negative_prompt", "negative_prompt_base", "负面规避词", "负面规避", "负面规避基础") or "",
        "ending_frame": _first_value(item, "ending_frame", "结尾画面", "收束画面") or "",
    }


def _normalize_storyboard_prompt_item(item: Dict[str, Any], index: int) -> Dict[str, Any]:
    return {
        **item,
        "shot_number": _first_value(item, "shot_number", "分镜序号") or index,
        "storyboard_image_prompt": _first_value(
            item,
            "storyboard_image_prompt",
            "image_prompt",
            "故事板图像提示词",
            "图像提示词",
        )
        or "",
        "video_prompt": _first_value(item, "video_prompt", "视频提示词") or "",
    }


def _normalize_storyboard_image_prompt_item(item: Dict[str, Any], index: int) -> Dict[str, Any]:
    return {
        **item,
        "shot_number": _first_value(item, "shot_number", "分镜序号") or index,
        "storyboard_image_prompt": _first_value(
            item,
            "storyboard_image_prompt",
            "image_prompt",
            "故事板提示词",
            "故事板图像提示词",
            "图像提示词",
        )
        or "",
    }


def _make_storyboard_from_item(
    *,
    project_id: UUID,
    chapter_id: UUID,
    user_id: UUID,
    ai_model_id: Optional[UUID],
    item: Dict[str, Any],
    index: int,
    extra: Optional[Dict[str, Any]] = None,
) -> ProjectStoryboard:
    event_goal = _optional_str(item.get("event_goal"))
    split_reason = _optional_str(item.get("split_reason"))
    item_extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    return ProjectStoryboard(
        project_id=project_id,
        chapter_id=chapter_id,
        user_id=user_id,
        ai_model_id=ai_model_id,
        shot_number=_as_int(item.get("shot_number"), index),
        title=str(item.get("title") or f"分镜{index}")[:128],
        source_content=str(item.get("source_content") or ""),
        scene_name=_optional_str(item.get("scene_name"), 128),
        scene_time=_optional_str(item.get("scene_time"), 64),
        scene_state=_optional_str(item.get("scene_state"), 128),
        shot_size=_optional_str(item.get("shot_size"), 64),
        camera_angle=_optional_str(item.get("camera_angle"), 128),
        camera_movement=_optional_str(item.get("camera_movement")),
        screen_execution=_optional_str(item.get("screen_execution")),
        characters=_as_string_list(item.get("characters")),
        props=_as_string_list(item.get("props")),
        action=_optional_str(_first_value(item, "core_action", "action")),
        character_action=_optional_str(item.get("character_action")),
        character_expression=_optional_str(item.get("character_expression")),
        dialogue=_optional_str(item.get("dialogue")),
        sound_effect=_optional_str(item.get("sound_effect")),
        atmosphere=_optional_str(item.get("atmosphere")),
        emotion=_optional_str(item.get("emotion")),
        visual_description=_optional_str(item.get("visual_description")),
        image_prompt=_optional_str(item.get("image_prompt")),
        video_prompt=_optional_str(item.get("video_prompt")),
        duration_suggestion=_optional_str(item.get("duration_suggestion"), 64),
        production_focus=_optional_str(item.get("production_focus")),
        negative_prompt=_optional_str(item.get("negative_prompt")),
        ending_frame=_optional_str(item.get("ending_frame")),
        extra={
            **item_extra,
            **(extra or {}),
            **({"event_goal": event_goal} if event_goal else {}),
            **({"split_reason": split_reason} if split_reason else {}),
            "raw_item": item,
        },
        is_enabled=True,
    )


def parse_storyboard_items(content: str) -> List[Dict[str, Any]]:
    payload = _parse_json_payload(content)
    items = _extract_storyboard_items(payload)
    if not isinstance(items, list):
        return []
    return [_normalize_storyboard_item(item, index) for index, item in enumerate(items, start=1) if isinstance(item, dict)]


async def _dump_storyboard_assets(db: AsyncSession, model: Any, project_id: UUID, user_id: UUID) -> str:
    result = await db.execute(
        select(model)
        .where(
            model.project_id == project_id,
            model.user_id == user_id,
            model.is_enabled.is_(True),
        )
        .order_by(model.created_at.asc())
    )
    assets = [_storyboard_asset_payload(asset) for asset in result.scalars().all()]
    return json.dumps(assets, ensure_ascii=False)


async def _list_enabled_storyboards(
    db: AsyncSession,
    project_id: UUID,
    chapter_id: UUID,
    user_id: UUID,
) -> List[ProjectStoryboard]:
    result = await db.execute(
        select(ProjectStoryboard)
        .where(
            ProjectStoryboard.project_id == project_id,
            ProjectStoryboard.chapter_id == chapter_id,
            ProjectStoryboard.user_id == user_id,
            ProjectStoryboard.is_enabled.is_(True),
        )
        .order_by(ProjectStoryboard.shot_number.asc(), ProjectStoryboard.created_at.asc())
    )
    return list(result.scalars().all())


def _storyboard_asset_payload(asset: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "name": asset.name,
        "prompt": getattr(asset, "prompt", None) or "",
        "description": getattr(asset, "description", None) or "",
    }
    if isinstance(asset, ProjectCharacter):
        payload.update(
            {
                "aliases": asset.aliases or [],
                "identity": asset.identity or "",
                "appearance": asset.appearance or "",
            }
        )
    elif isinstance(asset, ProjectScene):
        payload.update(
            {
                "location": asset.location or "",
                "time_of_day": asset.time_of_day or "",
                "environment": asset.environment or "",
            }
        )
    elif isinstance(asset, ProjectProp):
        payload.update(
            {
                "category": asset.category or "",
                "appearance": asset.appearance or "",
                "function": asset.function or "",
            }
        )
    return payload


def _storyboard_unit_payload(storyboard: ProjectStoryboard) -> Dict[str, Any]:
    return {
        "shot_number": storyboard.shot_number,
        "title": storyboard.title,
        "source_content": storyboard.source_content,
        "event_goal": storyboard.event_goal or "",
        "scene_name": storyboard.scene_name or "",
        "characters": storyboard.characters or [],
        "props": storyboard.props or [],
        "core_action": storyboard.action or "",
        "dialogue": storyboard.dialogue or "",
        "split_reason": storyboard.split_reason or "",
    }


def _storyboard_execution_payload(storyboard: ProjectStoryboard) -> Dict[str, Any]:
    return {
        "shot_number": storyboard.shot_number,
        "title": storyboard.title,
        "source_content": storyboard.source_content,
        "scene_name": storyboard.scene_name or "",
        "scene_state": storyboard.scene_state or "",
        "characters": storyboard.characters or [],
        "props": storyboard.props or [],
        "action": storyboard.action or "",
        "shot_size": storyboard.shot_size or "",
        "camera_angle": storyboard.camera_angle or "",
        "camera_movement": storyboard.camera_movement or "",
        "screen_execution": storyboard.screen_execution or "",
        "character_action": storyboard.character_action or "",
        "character_expression": storyboard.character_expression or "",
        "dialogue": storyboard.dialogue or "",
        "sound_effect": storyboard.sound_effect or "",
        "atmosphere": storyboard.atmosphere or "",
        "emotion": storyboard.emotion or "",
        "visual_description": storyboard.visual_description or "",
        "duration_suggestion": storyboard.duration_suggestion or "",
        "production_focus": storyboard.production_focus or storyboard.production_focus_base or "",
        "negative_prompt": storyboard.negative_prompt or storyboard.negative_prompt_base or "",
        "ending_frame": storyboard.ending_frame or "",
    }


def _storyboard_has_refinement(storyboard: ProjectStoryboard) -> bool:
    return any(
        (
            storyboard.shot_size,
            storyboard.camera_angle,
            storyboard.camera_movement,
            storyboard.screen_execution,
            storyboard.visual_description,
            storyboard.production_focus_base,
            storyboard.production_focus,
        )
    )


def _storyboard_stage_extra(storyboard: Optional[ProjectStoryboard]) -> Dict[str, Any]:
    if storyboard is None:
        return {}
    return {
        "storyboard_id": str(storyboard.id),
        "shot_number": storyboard.shot_number,
        "storyboard_title": storyboard.title,
    }


def _storyboard_stage_task_title(
    title_prefix: str,
    chapter: ProjectChapter,
    storyboard: Optional[ProjectStoryboard],
) -> str:
    if storyboard is None:
        return f"{title_prefix}：{chapter.title}"
    return f"{title_prefix}：{chapter.title} / 第{storyboard.shot_number}镜"


def _storyboard_stage_points_remark(
    title_prefix: str,
    chapter: ProjectChapter,
    storyboard: Optional[ProjectStoryboard],
) -> str:
    if storyboard is None:
        return f"项目章节{title_prefix}：{chapter.title}"
    return f"项目分镜{title_prefix}：{chapter.title} / 第{storyboard.shot_number}镜"


def _task_storyboard_id(task_record: UserTaskRecord) -> Optional[UUID]:
    storyboard_id = (task_record.extra or {}).get("storyboard_id")
    if not storyboard_id:
        return None
    try:
        return UUID(str(storyboard_id))
    except ValueError:
        return None


def _storyboard_stage_keys(generation_type: str) -> Tuple[str, str]:
    if generation_type == "storyboard_refinement":
        return "storyboard_refinement_status", "storyboard_refinement_task_record_id"
    if generation_type == "storyboard_image_prompt_generation":
        return "storyboard_image_prompt_generation_status", "storyboard_image_prompt_generation_task_record_id"
    if generation_type == "storyboard_prompt_generation":
        return "storyboard_prompt_generation_status", "storyboard_prompt_generation_task_record_id"
    return "storyboard_analysis_status", "storyboard_analysis_task_record_id"


def _storyboard_stage_title(generation_type: str) -> str:
    if generation_type == "storyboard_refinement":
        return "分镜细化与视频提示词生成"
    if generation_type == "storyboard_image_prompt_generation":
        return "故事板提示词生成"
    if generation_type == "storyboard_prompt_generation":
        return "视频提示词生成"
    return "分镜制作"


def _clear_task_retry_state(extra: Dict[str, Any]) -> Dict[str, Any]:
    cleaned = dict(extra)
    cleaned.pop("retry_reason", None)
    cleaned.pop("next_poll_seconds", None)
    return cleaned


def _clear_status_retry_state(extra: Dict[str, Any], status_key: str) -> Dict[str, Any]:
    cleaned = dict(extra)
    cleaned.pop(status_key.replace("_status", "_retry_reason"), None)
    return cleaned


def _build_merged_storyboard_item(
    storyboards: List[ProjectStoryboard],
    payload: ProjectStoryboardMergeRequest,
) -> Dict[str, Any]:
    first = storyboards[0]
    source_content = payload.source_content or _join_texts(storyboard.source_content for storyboard in storyboards)
    core_action = payload.core_action or payload.action or _join_texts(storyboard.action for storyboard in storyboards)
    split_reason = payload.split_reason or "用户判断所选分镜属于同一连续事件，合并为一个分镜。"
    return {
        "shot_number": first.shot_number,
        "title": payload.title or _merged_title(storyboards),
        "source_content": source_content,
        "event_goal": payload.event_goal or _join_texts(storyboard.event_goal for storyboard in storyboards) or "推进连续事件",
        "scene_name": payload.scene_name or _first_nonempty(storyboard.scene_name for storyboard in storyboards),
        "characters": payload.characters if payload.characters is not None else _merge_string_lists(storyboard.characters for storyboard in storyboards),
        "props": payload.props if payload.props is not None else _merge_string_lists(storyboard.props for storyboard in storyboards),
        "core_action": core_action,
        "dialogue": payload.dialogue if payload.dialogue is not None else _join_texts(storyboard.dialogue for storyboard in storyboards),
        "split_reason": split_reason,
        "extra": payload.extra or {},
    }


def _merged_title(storyboards: List[ProjectStoryboard]) -> str:
    if len(storyboards) == 2:
        return f"{storyboards[0].title}合并"[:128]
    return f"{storyboards[0].title}等合并"[:128]


def _assign_shot_numbers(storyboards: List[ProjectStoryboard]) -> None:
    now = beijing_datetime()
    for index, storyboard in enumerate(storyboards, start=1):
        storyboard.shot_number = index
        storyboard.updated_at = now


def _unique_uuids(values: List[UUID]) -> List[UUID]:
    seen: set[UUID] = set()
    unique_values: List[UUID] = []
    for value in values:
        if value not in seen:
            unique_values.append(value)
            seen.add(value)
    return unique_values


def _join_texts(values: Any) -> str:
    texts: List[str] = []
    seen: set[str] = set()
    for value in values:
        if value in (None, ""):
            continue
        text = str(value).strip()
        if text and text not in seen:
            texts.append(text)
            seen.add(text)
    return "\n".join(texts)


def _first_nonempty(values: Any) -> str:
    for value in values:
        if value not in (None, ""):
            return str(value)
    return ""


def _merge_string_lists(values: Any) -> List[str]:
    merged: List[str] = []
    seen: set[str] = set()
    for value in values:
        for item in _as_string_list(value):
            if item not in seen:
                merged.append(item)
                seen.add(item)
    return merged


def _normalize_storyboard_item(item: Dict[str, Any], index: int) -> Dict[str, Any]:
    description_prompt = str(_first_value(item, "description_prompt", "画面描述", "视频提示词") or "")
    scenes = _as_string_list(_first_value(item, "scenes", "场景", "场景名称"))
    return {
        **item,
        "shot_number": _first_value(item, "shot_number", "storyboard_index", "分镜序号", "镜头编号") or index,
        "title": _first_value(item, "title", "标题") or f"分镜{_first_value(item, 'storyboard_index') or index}",
        "source_content": _first_value(item, "source_content", "original_text", "原文", "原始文本") or "",
        "event_goal": _first_value(item, "event_goal", "叙事目标", "事件目标") or "",
        "scene_name": _first_value(item, "scene_name", "scene", "场景名称") or (scenes[0] if scenes else ""),
        "scene_time": "",
        "scene_state": "",
        "shot_size": "",
        "camera_angle": "",
        "camera_movement": "",
        "screen_execution": "",
        "characters": _first_value(item, "characters", "角色", "人物") or [],
        "props": _first_value(item, "props", "道具") or [],
        "core_action": _first_value(item, "core_action", "核心动作") or _first_value(item, "action", "动作") or description_prompt,
        "action": _first_value(item, "action", "动作") or _first_value(item, "core_action", "核心动作") or description_prompt,
        "character_action": "",
        "character_expression": "",
        "dialogue": _first_value(item, "dialogue", "台词") or "",
        "sound_effect": "",
        "atmosphere": "",
        "emotion": "",
        "visual_description": "",
        "image_prompt": "",
        "video_prompt": "",
        "duration_suggestion": "",
        "production_focus": "",
        "negative_prompt": "",
        "ending_frame": "",
        "split_reason": _first_value(item, "split_reason", "拆分理由") or "",
    }


def _extract_storyboard_items(payload: Any) -> Optional[List[Any]]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return None
    for key in ("storyboard_units", "items", "shots", "storyboards", "分镜", "分镜列表"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict) and _looks_like_storyboard_item(value):
            return [value]
    data = payload.get("data") or payload.get("result")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        nested_items = _extract_storyboard_items(data)
        if nested_items:
            return nested_items
    if _looks_like_storyboard_item(payload):
        return [payload]
    for value in payload.values():
        if isinstance(value, list) and any(isinstance(item, dict) for item in value):
            return value
        if isinstance(value, dict):
            nested_items = _extract_storyboard_items(value)
            if nested_items:
                return nested_items
    return None


def _looks_like_storyboard_item(value: Dict[str, Any]) -> bool:
    keys = set(value.keys())
    storyboard_keys = {
        "shot_number",
        "storyboard_index",
        "title",
        "source_content",
        "original_text",
        "event_goal",
        "scene_name",
        "core_action",
        "split_reason",
        "scene_time",
        "scene_state",
        "shot_size",
        "camera_angle",
        "camera_movement",
        "screen_execution",
        "character_action",
        "character_expression",
        "atmosphere",
        "visual_description",
        "storyboard_image_prompt",
        "image_prompt",
        "video_prompt",
        "duration_suggestion",
        "production_focus",
        "negative_prompt",
        "ending_frame",
        "分镜序号",
        "镜头编号",
        "标题",
        "叙事目标",
        "核心动作",
        "拆分理由",
        "场景时间",
        "场景状态",
        "景别",
        "拍摄角度",
        "运镜",
        "画面执行",
        "角色动作",
        "角色表情",
        "氛围参考",
        "画面氛围",
        "结尾画面",
        "收束画面",
        "故事板提示词",
        "故事板图像提示词",
        "图像提示词",
        "视频提示词",
    }
    return bool(keys & storyboard_keys)


def _parse_json_payload(content: str) -> Any:
    content = _strip_json_code_fence(content or "")
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for match in re.finditer(r"[\{\[]", content):
        try:
            payload, _ = decoder.raw_decode(content[match.start() :])
            return payload
        except json.JSONDecodeError:
            continue
    return {}


def _strip_json_code_fence(content: str) -> str:
    content = content.strip()
    match = re.search(r"```(?:json)?\s*(.*?)```", content, re.S | re.I)
    return match.group(1).strip() if match else content


def _first_value(item: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return None


def _as_int(value: Any, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _as_string_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item not in (None, "")]


def _optional_str(value: Any, max_length: Optional[int] = None) -> Optional[str]:
    if value in (None, ""):
        return None
    text = str(value)
    return text[:max_length] if max_length else text


async def _mark_storyboard_enqueue_failed(
    db: AsyncSession,
    task_record: UserTaskRecord,
    chapter: ProjectChapter,
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
        "refund_transaction_id": refund_transaction_id,
    }
    chapter.extra = {
        **(chapter.extra or {}),
        "storyboard_analysis_status": "failed",
        "storyboard_analysis_failed_reason": "任务入队失败",
    }
    await db.commit()


async def _mark_storyboard_stage_enqueue_failed(
    db: AsyncSession,
    task_record: UserTaskRecord,
    chapter: ProjectChapter,
    status_key: str,
    storyboard: Optional[ProjectStoryboard] = None,
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
        "refund_transaction_id": refund_transaction_id,
    }
    chapter.extra = {
        **(chapter.extra or {}),
        status_key: "failed",
        status_key.replace("_status", "_failed_reason"): "任务入队失败",
    }
    if storyboard is not None:
        storyboard.extra = {
            **(storyboard.extra or {}),
            status_key: "failed",
            status_key.replace("_status", "_failed_reason"): "任务入队失败",
        }
    await db.commit()
