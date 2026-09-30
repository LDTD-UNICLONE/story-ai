"""分镜 Worker 执行与结果落库；保留任务锁、终态检查和结算，事务由调用方提交。"""

import logging
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException
from app.core.logging import log_extra
from app.core.timezone import beijing_datetime
from app.models.ai_model import AiModel
from app.models.project_chapter import ProjectChapter
from app.models.project_storyboard import ProjectStoryboard
from app.models.task_record import UserTaskRecord
from app.services.billing.model_points import (
    settle_text_task_points,
)
from app.services.models.configuration import build_model_runtime_snapshot
from app.services.generation.runner import run_model
from app.services.billing.points import change_user_points
from app.services.generation.task_execution import lock_active_task
from app.services.projects.storyboard_parsing import (
    _as_int,
    _as_string_list,
    _first_value,
    _is_storyboard_image_prompt_generation,
    _optional_str,
    prepare_agent_storyboard_groups,
    validate_agent_storyboard_sequence,
    parse_storyboard_items,
    parse_storyboard_stage_items,
)
from app.services.projects.storyboards import (
    list_enabled_storyboards,
    make_storyboard_from_item,
    get_project_storyboard_or_404,
)


logger = logging.getLogger(__name__)


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

    model_snapshot = build_model_runtime_snapshot(ai_model)
    model_result = await run_model(
        model_snapshot,
        "text",
        task_record.prompt,
        (task_record.extra or {}).get("model_extra") or {},
        idempotency_key=str(task_record.id),
    )
    if not await lock_active_task(db, task_record):
        return
    await db.refresh(chapter, with_for_update=True)
    if not storyboard_analysis_task_is_current(task_record, chapter):
        await mark_storyboard_analysis_task_superseded(db, task_record)
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
    if (task_record.extra or {}).get("agent_production_id"):
        validate_agent_storyboard_sequence(items, chapter.processed_content)
        items = prepare_agent_storyboard_groups(
            items,
            str((task_record.extra or {}).get("agent_visual_style") or ""),
        )

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
    created_storyboards: List[ProjectStoryboard] = []
    for index, item in enumerate(items, start=1):
        item = {**item, "shot_number": index}
        storyboard = make_storyboard_from_item(
            project_id=task_record.business_id,
            chapter_id=chapter.id,
            user_id=task_record.user_id,
            ai_model_id=task_record.ai_model_id,
            item=item,
            index=index,
            extra={"task_record_id": str(task_record.id)},
        )
        db.add(storyboard)
        created_storyboards.append(storyboard)

    agent_production_id = (task_record.extra or {}).get("agent_production_id")
    if agent_production_id:
        await db.flush()
        from app.services.agent.storyboard_bindings import (
            bind_agent_storyboard_analysis_result,
        )

        await bind_agent_storyboard_analysis_result(
            db,
            UUID(str(agent_production_id)),
            created_storyboards,
        )

    chapter.extra = {
        **_clear_status_retry_state(chapter.extra or {}, "storyboard_analysis_status"),
        "storyboard_analysis_status": "success",
        "storyboard_analysis_task_record_id": str(task_record.id),
        **(
            {
                "storyboard_analysis_result_fingerprint": str(
                    (task_record.extra or {}).get("storyboard_analysis_input_fingerprint")
                )
            }
            if (task_record.extra or {}).get("storyboard_analysis_input_fingerprint")
            else {}
        ),
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


def storyboard_analysis_task_is_current(
    task_record: UserTaskRecord,
    chapter: ProjectChapter,
) -> bool:
    task_extra = task_record.extra or {}
    if not task_extra.get("agent_production_id"):
        return True
    chapter_extra = chapter.extra or {}
    task_fingerprint = str(task_extra.get("storyboard_analysis_input_fingerprint") or "")
    expected_fingerprint = str(chapter_extra.get("storyboard_analysis_input_fingerprint") or "")
    current_task_id = str(chapter_extra.get("storyboard_analysis_task_record_id") or "")
    return bool(
        task_fingerprint
        and expected_fingerprint == task_fingerprint
        and current_task_id == str(task_record.id)
    )


async def mark_storyboard_analysis_task_superseded(
    db: AsyncSession,
    task_record: UserTaskRecord,
) -> None:
    refund_transaction_id = (task_record.extra or {}).get("refund_transaction_id")
    if task_record.points_cost > 0 and not refund_transaction_id:
        refund = await change_user_points(
            db,
            user_id=task_record.user_id,
            amount=task_record.points_cost,
            transaction_type="refund",
            remark=f"分镜输入已更新退回积分：{task_record.title}",
            auto_commit=False,
        )
        refund_transaction_id = str(refund.id)
    task_record.status = "failed"
    task_record.result = "分镜输入已更新，本次旧任务结果已忽略"
    task_record.extra = {
        **_clear_task_retry_state(task_record.extra or {}),
        "superseded": True,
        "failed_reason": "分镜输入已更新，本次旧任务结果已忽略",
        "refund_transaction_id": refund_transaction_id,
    }


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

    model_snapshot = build_model_runtime_snapshot(ai_model)
    model_result = await run_model(
        model_snapshot,
        "text",
        task_record.prompt,
        (task_record.extra or {}).get("model_extra") or {},
        idempotency_key=str(task_record.id),
    )
    if not await lock_active_task(db, task_record):
        return

    await db.refresh(chapter)
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
    elif _is_storyboard_image_prompt_generation(task_record.generation_type):
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

    storyboards = await list_enabled_storyboards(
        db, task_record.business_id, chapter.id, task_record.user_id
    )
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

    storyboards = await list_enabled_storyboards(
        db, task_record.business_id, chapter.id, task_record.user_id
    )
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
            raise AppException(
                "单个故事板提示词结果必须只包含一条分镜", code=50231, status_code=502
            )
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
    storyboard.video_prompt = None
    storyboard.duration_suggestion = _optional_str(item.get("duration_suggestion"), 64)
    storyboard.production_focus = _optional_str(item.get("production_focus"))
    storyboard.negative_prompt = _optional_str(item.get("negative_prompt"))
    storyboard.ending_frame = _optional_str(
        _first_value(item, "ending_frame", "结尾画面", "收束画面")
    )
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
    storyboard.image_prompt = _optional_str(_first_value(item, "image_prompt", "图像提示词"))
    storyboard.video_prompt = _optional_str(_first_value(item, "video_prompt", "视频提示词"))
    storyboard.duration_suggestion = _optional_str(
        _first_value(item, "duration_suggestion", "时长建议"), 64
    )
    storyboard.negative_prompt = _optional_str(
        _first_value(item, "negative_prompt", "负面规避词", "负面规避")
    )
    storyboard.extra = {
        **_clear_status_retry_state(
            storyboard.extra or {}, "storyboard_image_prompt_generation_status"
        ),
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
    storyboard.image_prompt = _optional_str(item.get("image_prompt"))
    storyboard.video_prompt = _optional_str(_first_value(item, "video_prompt", "视频提示词"))
    storyboard.extra = {
        **_clear_status_retry_state(storyboard.extra or {}, "storyboard_prompt_generation_status"),
        "storyboard_prompt_generation_status": "success",
        "storyboard_prompt_generation_task_record_id": str(task_record.id),
        "prompt_generation_task_record_id": str(task_record.id),
        "prompt_generation_raw_item": item,
    }
    storyboard.updated_at = now


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
    if _is_storyboard_image_prompt_generation(generation_type):
        return (
            "storyboard_image_prompt_generation_status",
            "storyboard_image_prompt_generation_task_record_id",
        )
    if generation_type == "storyboard_prompt_generation":
        return "storyboard_prompt_generation_status", "storyboard_prompt_generation_task_record_id"
    return "storyboard_analysis_status", "storyboard_analysis_task_record_id"


def _storyboard_stage_title(generation_type: str) -> str:
    if generation_type == "storyboard_refinement":
        return "分镜细化字段生成"
    if _is_storyboard_image_prompt_generation(generation_type):
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
