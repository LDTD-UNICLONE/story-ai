import json
import re
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException
from app.core.timezone import beijing_datetime
from app.models.ai_model import AiModel
from app.models.project_asset import ProjectCharacter, ProjectProp, ProjectScene
from app.models.project_chapter import ProjectChapter
from app.models.project_storyboard import ProjectStoryboard
from app.models.task_record import UserTaskRecord
from app.models.user import User
from app.schemas.project_storyboard import ProjectStoryboardAnalyzeRequest, ProjectStoryboardUpdateRequest
from app.services.model_points import calculate_model_points_cost
from app.services.model_runner import run_model
from app.services.points import change_user_points, consume_user_points
from app.services.project_chapter_processing import get_enabled_text_model_or_404
from app.services.project_chapters import get_project_chapter_or_404
from app.services.prompts import render_system_prompt
from app.services.projects import get_project_or_404
from app.services.task_records import create_user_task_record, reconcile_provider_task_result


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
    await reconcile_provider_task_result(db, task_record)
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
    for field, value in update_data.items():
        setattr(storyboard, field, value)
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
    points_cost = calculate_model_points_cost(ai_model)
    points_transaction = None
    if points_cost > 0:
        points_transaction = await consume_user_points(
            db,
            user_id=user.id,
            amount=points_cost,
            remark=f"项目章节分镜分析：{chapter.title}",
            auto_commit=False,
        )

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
            "model_extra": payload.extra or {},
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
    items = parse_storyboard_items(model_result.content)
    if not items:
        raise AppException("分镜分析未返回有效数据", code=50231, status_code=502)

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
        storyboard = ProjectStoryboard(
            project_id=task_record.business_id,
            chapter_id=chapter.id,
            user_id=task_record.user_id,
            ai_model_id=task_record.ai_model_id,
            shot_number=_as_int(item.get("shot_number"), index),
            title=str(item.get("title") or f"分镜{index}")[:128],
            source_content=str(item.get("source_content") or ""),
            scene_name=_optional_str(item.get("scene_name"), 128),
            scene_time=_optional_str(item.get("scene_time"), 64),
            shot_size=_optional_str(item.get("shot_size"), 64),
            camera_angle=_optional_str(item.get("camera_angle"), 128),
            camera_movement=_optional_str(item.get("camera_movement")),
            screen_execution=_optional_str(item.get("screen_execution")),
            characters=_as_string_list(item.get("characters")),
            props=_as_string_list(item.get("props")),
            action=_optional_str(item.get("action")),
            character_action=_optional_str(item.get("character_action")),
            character_expression=_optional_str(item.get("character_expression")),
            dialogue=_optional_str(item.get("dialogue")),
            sound_effect=_optional_str(item.get("sound_effect")),
            emotion=_optional_str(item.get("emotion")),
            visual_description=_optional_str(item.get("visual_description")),
            image_prompt=_optional_str(item.get("image_prompt")),
            video_prompt=_optional_str(item.get("video_prompt")),
            duration_suggestion=_optional_str(item.get("duration_suggestion"), 64),
            production_focus=_optional_str(item.get("production_focus")),
            negative_prompt=_optional_str(item.get("negative_prompt")),
            extra={"task_record_id": str(task_record.id), "raw_item": item},
            is_enabled=True,
        )
        db.add(storyboard)

    chapter.extra = {
        **(chapter.extra or {}),
        "storyboard_analysis_status": "success",
        "storyboard_analysis_task_record_id": str(task_record.id),
    }
    task_record.status = "success"
    task_record.result = model_result.content
    task_record.extra = {
        **(task_record.extra or {}),
        "model_result_extra": model_result.extra,
        "storyboard_count": len(items),
    }


def parse_storyboard_items(content: str) -> List[Dict[str, Any]]:
    payload = _parse_json_object(content)
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        items = payload.get("shots") if isinstance(payload, dict) else None
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


def _normalize_storyboard_item(item: Dict[str, Any], index: int) -> Dict[str, Any]:
    description_prompt = str(item.get("description_prompt") or "")
    scenes = _as_string_list(item.get("scenes"))
    duration = item.get("video_duration_seconds")
    if duration not in (None, ""):
        duration_suggestion = f"{_as_int(duration, 5)}s"
    else:
        duration_suggestion = str(item.get("duration_suggestion") or "")
    return {
        **item,
        "shot_number": item.get("shot_number") or item.get("storyboard_index") or index,
        "title": item.get("title") or f"分镜{item.get('storyboard_index') or index}",
        "source_content": item.get("source_content") or item.get("original_text") or "",
        "scene_name": item.get("scene_name") or (scenes[0] if scenes else ""),
        "scene_time": item.get("scene_time") or "",
        "shot_size": item.get("shot_size") or "",
        "camera_angle": item.get("camera_angle") or "",
        "camera_movement": item.get("camera_movement") or "",
        "screen_execution": item.get("screen_execution") or "",
        "characters": item.get("characters") or [],
        "props": item.get("props") or [],
        "action": item.get("action") or description_prompt,
        "character_action": item.get("character_action") or item.get("action") or "",
        "character_expression": item.get("character_expression") or item.get("emotion") or "",
        "dialogue": item.get("dialogue") or "",
        "sound_effect": item.get("sound_effect") or "",
        "emotion": item.get("emotion") or "",
        "visual_description": item.get("visual_description") or description_prompt,
        "image_prompt": item.get("image_prompt") or "",
        "video_prompt": item.get("video_prompt") or description_prompt,
        "duration_suggestion": duration_suggestion,
        "production_focus": item.get("production_focus") or "",
        "negative_prompt": item.get("negative_prompt") or "",
    }


def _parse_json_object(content: str) -> Dict[str, Any]:
    try:
        payload = json.loads(content)
        return payload if isinstance(payload, dict) else {}
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content or "", re.S)
        if not match:
            return {}
        try:
            payload = json.loads(match.group(0))
            return payload if isinstance(payload, dict) else {}
        except json.JSONDecodeError:
            return {}


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
