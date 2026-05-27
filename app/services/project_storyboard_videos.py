import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.exceptions import AppException
from app.core.timezone import beijing_datetime
from app.integrations.volcengine_ark_video_specs import VOLCENGINE_ARK_VENDOR, is_volcengine_ark_video_model
from app.models.ai_model import AiModel
from app.models.project import Project
from app.models.project_asset import ProjectCharacter, ProjectProp, ProjectScene
from app.models.project_storyboard import ProjectStoryboard
from app.models.task_record import UserTaskRecord
from app.models.user import User
from app.schemas.project_storyboard import ProjectStoryboardVideoGenerateRequest
from app.services.generated_media import persist_generated_media_to_oss
from app.services.model_points import calculate_submission_points_cost, settle_video_task_points
from app.services.model_runner import ModelRunResult, query_model_task, run_model
from app.services.points import change_user_points, consume_user_points
from app.services.project_storyboards import get_project_storyboard_or_404
from app.services.projects import get_project_or_404
from app.services.task_records import create_user_task_record, refresh_task_record_interrupted


MODE_TO_PROVIDER_MODE = {
    "reference": "image_to_video",
    "first_last_frame": "first_last_frame",
    "storyboard": "storyboard",
}


async def submit_storyboard_video_generation(
    db: AsyncSession,
    project_id: UUID,
    chapter_id: UUID,
    storyboard_id: UUID,
    user: User,
    payload: ProjectStoryboardVideoGenerateRequest,
) -> Tuple[UserTaskRecord, int]:
    await get_project_or_404(db, project_id, user.id)
    storyboard = await get_project_storyboard_or_404(db, project_id, chapter_id, storyboard_id, user.id)
    project = await _get_project_with_style_or_404(db, project_id, user.id)
    ai_model = await _get_enabled_video_model_or_404(db, payload.ai_model_id)

    reference_images = await _collect_reference_images(db, project_id, user.id, payload)
    reference_images, dropped_reference_images = _limit_reference_images_for_model(ai_model, payload, reference_images)
    model_extra = _build_storyboard_video_extra(project, ai_model, payload, reference_images)
    prompt = _build_storyboard_video_prompt(project, storyboard, payload.prompt)
    points_cost = calculate_submission_points_cost(ai_model, "video", model_extra)

    points_transaction = None
    if points_cost > 0:
        points_transaction = await consume_user_points(
            db,
            user_id=user.id,
            amount=points_cost,
            remark=f"分镜视频生成：{storyboard.title}",
            auto_commit=False,
        )

    task_record = await create_user_task_record(
        db,
        user_id=user.id,
        ai_model_id=ai_model.id,
        points_transaction_id=points_transaction.id if points_transaction else None,
        business_type="project",
        business_id=project_id,
        generation_type="storyboard_video",
        status="pending",
        title=f"分镜视频生成：{storyboard.title}",
        prompt=prompt,
        result=None,
        points_cost=points_cost,
        extra={
            "project_id": str(project_id),
            "chapter_id": str(chapter_id),
            "storyboard_id": str(storyboard_id),
            "generation_mode": payload.generation_mode,
            "reference_images": reference_images,
            "dropped_reference_images": dropped_reference_images,
            "first_frame_url": payload.first_frame_url,
            "last_frame_url": payload.last_frame_url,
            "model_extra": model_extra,
        },
    )
    storyboard.extra = {
        **(storyboard.extra or {}),
        "video_generation_status": "pending",
        "video_generation_task_record_id": str(task_record.id),
        "video_generation_mode": payload.generation_mode,
    }
    await db.commit()

    try:
        from app.tasks.project_storyboard_video import run_project_storyboard_video_generation

        run_project_storyboard_video_generation.delay(str(task_record.id), str(storyboard_id))
    except Exception:
        await _mark_storyboard_video_enqueue_failed(db, task_record, storyboard)
    return task_record, points_cost


async def run_storyboard_video_generation_in_worker(
    db: AsyncSession,
    task_record: UserTaskRecord,
    storyboard_id: UUID,
) -> None:
    storyboard = await db.get(ProjectStoryboard, storyboard_id)
    if storyboard is None or not storyboard.is_enabled:
        raise AppException("项目分镜不存在", code=40410, status_code=404)

    result = await db.execute(
        select(AiModel).where(
            AiModel.id == task_record.ai_model_id,
            AiModel.model_type == "video",
            AiModel.is_enabled.is_(True),
        )
    )
    ai_model = result.scalar_one_or_none()
    if ai_model is None:
        raise AppException("视频模型不存在或已禁用", code=40404, status_code=404)

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
        "video",
        task_record.prompt,
        (task_record.extra or {}).get("model_extra") or {},
    )
    model_result = await _resolve_video_provider_task(model_snapshot, model_result)
    model_result = await persist_generated_media_to_oss("video", model_result)
    if await refresh_task_record_interrupted(db, task_record):
        return

    if model_result.extra.get("platform_task_status") == "running":
        storyboard.extra = {
            **(storyboard.extra or {}),
            "video_generation_status": "running",
            "video_generation_task_record_id": str(task_record.id),
            "video_generation_result": model_result.content,
            "video_generation_extra": model_result.extra,
        }
        storyboard.updated_at = beijing_datetime()
        task_record.status = "running"
        task_record.result = model_result.content
        task_record.extra = {
            **(task_record.extra or {}),
            "model_result_extra": model_result.extra,
            "storyboard_video_result": model_result.content,
        }
        return

    if not model_result.content or model_result.content == "生成任务处理中":
        raise AppException("视频生成未返回有效结果", code=50231, status_code=502)

    storyboard.extra = {
        **(storyboard.extra or {}),
        "video_generation_status": model_result.extra.get("platform_task_status") or "success",
        "video_generation_task_record_id": str(task_record.id),
        "video_generation_result": model_result.content,
        "video_generation_extra": model_result.extra,
    }
    storyboard.updated_at = beijing_datetime()
    task_record.status = model_result.extra.get("platform_task_status") or "success"
    task_record.result = model_result.content
    task_record.extra = {
        **(task_record.extra or {}),
        "model_result_extra": model_result.extra,
        "storyboard_video_result": model_result.content,
    }
    await settle_video_task_points(
        db,
        task_record,
        ai_model,
        (task_record.extra or {}).get("model_extra") or {},
        remark_prefix="分镜视频生成",
    )


async def _get_project_with_style_or_404(db: AsyncSession, project_id: UUID, user_id: UUID) -> Project:
    result = await db.execute(
        select(Project)
        .options(selectinload(Project.style))
        .where(Project.id == project_id, Project.user_id == user_id, Project.is_enabled.is_(True))
    )
    project = result.scalar_one_or_none()
    if project is None:
        raise AppException("项目不存在", code=40407, status_code=404)
    return project


async def _get_enabled_video_model_or_404(db: AsyncSession, ai_model_id: UUID) -> AiModel:
    result = await db.execute(
        select(AiModel).where(
            AiModel.id == ai_model_id,
            AiModel.model_type == "video",
            AiModel.is_enabled.is_(True),
        )
    )
    ai_model = result.scalar_one_or_none()
    if ai_model is None:
        raise AppException("视频模型不存在、未启用或类型不匹配", code=40404, status_code=404)
    return ai_model


async def _collect_reference_images(
    db: AsyncSession,
    project_id: UUID,
    user_id: UUID,
    payload: ProjectStoryboardVideoGenerateRequest,
) -> List[str]:
    urls: List[str] = []
    urls.extend(payload.uploaded_images or [])
    urls.extend(await _asset_reference_images(db, ProjectCharacter, project_id, user_id, payload.character_ids))
    urls.extend(await _asset_reference_images(db, ProjectScene, project_id, user_id, payload.scene_ids))
    urls.extend(await _asset_reference_images(db, ProjectProp, project_id, user_id, payload.prop_ids))
    return _dedupe(urls)


def _limit_reference_images_for_model(
    ai_model: AiModel,
    payload: ProjectStoryboardVideoGenerateRequest,
    reference_images: List[str],
) -> Tuple[List[str], List[str]]:
    max_images = _model_image_limit(ai_model)
    if max_images <= 0:
        return reference_images, []

    reserved_count = 0
    if payload.generation_mode == "first_last_frame":
        reserved_count += 1 if payload.first_frame_url else 0
        reserved_count += 1 if payload.last_frame_url else 0

    allowed_reference_count = max(0, max_images - reserved_count)
    if len(reference_images) <= allowed_reference_count:
        return reference_images, []
    return reference_images[:allowed_reference_count], reference_images[allowed_reference_count:]


def _model_image_limit(ai_model: AiModel) -> int:
    media_limits = (ai_model.capabilities or {}).get("media_limits") or {}
    raw_limit = media_limits.get("images")
    if isinstance(raw_limit, int) and raw_limit > 0:
        return raw_limit
    if ai_model.vendor == VOLCENGINE_ARK_VENDOR or is_volcengine_ark_video_model(ai_model.model_id):
        return 9
    return 0


async def _asset_reference_images(
    db: AsyncSession,
    model: Any,
    project_id: UUID,
    user_id: UUID,
    asset_ids: List[UUID],
) -> List[str]:
    if not asset_ids:
        return []
    result = await db.execute(
        select(model.reference_image).where(
            model.id.in_(asset_ids),
            model.project_id == project_id,
            model.user_id == user_id,
            model.is_enabled.is_(True),
            model.reference_image.is_not(None),
        )
    )
    return [url for url in result.scalars().all() if url]


def _build_storyboard_video_extra(
    project: Project,
    ai_model: AiModel,
    payload: ProjectStoryboardVideoGenerateRequest,
    reference_images: List[str],
) -> Dict[str, Any]:
    extra = dict(payload.extra or {})
    extra.setdefault("aspect_ratio", project.generation_ratio)
    extra.setdefault("ratio", project.generation_ratio)
    extra["video_mode"] = MODE_TO_PROVIDER_MODE[payload.generation_mode]
    extra["capability"] = MODE_TO_PROVIDER_MODE[payload.generation_mode]
    if reference_images:
        extra["images"] = reference_images
        if ai_model.vendor != VOLCENGINE_ARK_VENDOR and not is_volcengine_ark_video_model(ai_model.model_id):
            extra["image_urls"] = reference_images
    if payload.generation_mode == "first_last_frame":
        media_items = list(extra.get("media_items") or extra.get("media") or [])
        if payload.first_frame_url:
            media_items.append({"type": "image_url", "image_url": {"url": payload.first_frame_url}, "role": "first_frame"})
        if payload.last_frame_url:
            media_items.append({"type": "image_url", "image_url": {"url": payload.last_frame_url}, "role": "last_frame"})
        if media_items:
            extra["media_items"] = media_items
    return extra


def _build_storyboard_video_prompt(
    project: Project,
    storyboard: ProjectStoryboard,
    custom_prompt: Optional[str],
) -> str:
    style_prompt = project.style.prompt if project.style else ""
    video_prompt = _build_rich_storyboard_video_prompt(storyboard)
    lines = [
        ("视频提示词", video_prompt),
        ("项目风格", style_prompt),
        ("场景时间", storyboard.scene_time or ""),
        ("景别", storyboard.shot_size or ""),
        ("拍摄角度", storyboard.camera_angle or ""),
        ("运镜", storyboard.camera_movement or ""),
        ("画面执行", storyboard.screen_execution or ""),
        ("角色动作", storyboard.character_action or storyboard.action or ""),
        ("角色表情", storyboard.character_expression or storyboard.emotion or ""),
        ("台词", storyboard.dialogue or ""),
        ("音效", storyboard.sound_effect or ""),
        ("制作重点", storyboard.production_focus or ""),
        ("负面规避", storyboard.negative_prompt or ""),
        ("用户补充", custom_prompt or ""),
    ]
    return "\n".join(
        f"{label}：{value.strip()}"
        for label, value in lines
        if value and value.strip()
    )


def _build_rich_storyboard_video_prompt(storyboard: ProjectStoryboard) -> str:
    base_prompt = storyboard.video_prompt or storyboard.visual_description or ""
    if len(base_prompt.strip()) >= 180:
        return base_prompt
    supplements = [
        ("画面内容", storyboard.visual_description or ""),
        ("镜头运动", storyboard.camera_movement or ""),
        ("画面执行", storyboard.screen_execution or ""),
        ("角色动作", storyboard.character_action or storyboard.action or ""),
        ("角色表情", storyboard.character_expression or storyboard.emotion or ""),
    ]
    return "\n".join(
        item
        for item in [base_prompt.strip()]
        + [f"{label}：{value.strip()}" for label, value in supplements if value and value.strip()]
        if item
    )


async def _resolve_video_provider_task(model_snapshot: SimpleNamespace, model_result: ModelRunResult) -> ModelRunResult:
    task_id = model_result.extra.get("task_id")
    if not task_id:
        return model_result
    latest_result = model_result
    for _ in range(settings.provider_task_worker_poll_max_attempts):
        await asyncio.sleep(settings.provider_task_worker_poll_interval_seconds)
        latest_result = await query_model_task(model_snapshot, "video", str(task_id))
        status = str(latest_result.extra.get("task_status") or "").lower()
        if status in {"failed", "failure", "fail", "error", "canceled", "cancelled"}:
            raise AppException(f"模型任务执行失败：{status}", code=50231, status_code=502)
        if status in {"success", "succeeded", "completed", "complete", "finished", "done"}:
            latest_result.extra = {**latest_result.extra, "platform_task_status": "success"}
            return latest_result
        if status in {"not_start", "in_progress", "running", "pending", "processing", "queued"}:
            continue
        if latest_result.content and latest_result.content != "生成任务处理中":
            latest_result.extra = {**latest_result.extra, "platform_task_status": "success"}
            return latest_result
    latest_result.extra = {**latest_result.extra, "platform_task_status": "running", "provider_polling_timeout": True}
    latest_result.extra["next_poll_seconds"] = settings.provider_task_poll_interval_seconds
    latest_result.content = f"模型任务仍在生成中：{task_id}"
    return latest_result


async def _mark_storyboard_video_enqueue_failed(
    db: AsyncSession,
    task_record: UserTaskRecord,
    storyboard: ProjectStoryboard,
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
    storyboard.extra = {
        **(storyboard.extra or {}),
        "video_generation_status": "failed",
        "video_generation_failed_reason": "任务入队失败",
    }
    await db.commit()


def _dedupe(values: List[str]) -> List[str]:
    items: List[str] = []
    seen = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            items.append(value)
    return items
