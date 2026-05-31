import asyncio
import math
import re
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.exceptions import AppException
from app.core.timezone import beijing_datetime
from app.integrations.volcengine_ark_video_specs import (
    VOLCENGINE_ARK_VENDOR,
    is_volcengine_ark_video_model,
    merge_video_capabilities as merge_ark_video_capabilities,
    normalize_video_resolution,
)
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
from app.services.project_generated_assets import create_project_generated_asset_history, extract_result_urls
from app.services.project_storyboards import get_project_storyboard_or_404
from app.services.provider_polling import provider_poll_interval_seconds
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
    resolution = _normalize_storyboard_video_resolution(ai_model, payload.resolution)

    reference_images = await _collect_reference_images(db, project_id, user.id, payload)
    reference_start_frame_url = _reference_start_frame_url(payload)
    if reference_start_frame_url:
        reference_images = _dedupe([reference_start_frame_url, *reference_images])
    reference_images, dropped_reference_images = _limit_reference_images_for_model(ai_model, payload, reference_images)
    if reference_start_frame_url and reference_start_frame_url not in reference_images:
        reference_start_frame_url = ""
    model_extra = _build_storyboard_video_extra(
        project,
        ai_model,
        payload,
        storyboard,
        reference_images,
        resolution,
        reference_start_frame_url,
    )
    prompt = _build_storyboard_video_prompt(
        project,
        storyboard,
        payload.prompt,
        payload.generation_mode,
        payload.first_frame_url,
        payload.last_frame_url,
        reference_start_frame_url,
    )
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
            "resolution": resolution,
            "return_last_frame": payload.return_last_frame,
            "reference_start_frame_url": reference_start_frame_url,
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
        "video_generation_resolution": resolution,
        "video_generation_return_last_frame": payload.return_last_frame,
        **({"video_generation_reference_start_frame_url": reference_start_frame_url} if reference_start_frame_url else {}),
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
        last_frame_url = _first_generated_last_frame_url(model_result.extra)
        storyboard.extra = {
            **(storyboard.extra or {}),
            "video_generation_status": "running",
            "video_generation_task_record_id": str(task_record.id),
            "video_generation_result": model_result.content,
            "video_generation_extra": model_result.extra,
            **({"video_generation_last_frame_url": last_frame_url} if last_frame_url else {}),
        }
        storyboard.updated_at = beijing_datetime()
        task_record.status = "running"
        task_record.result = model_result.content
        task_record.extra = {
            **(task_record.extra or {}),
            "model_result_extra": model_result.extra,
            "storyboard_video_result": model_result.content,
            **({"storyboard_video_last_frame_url": last_frame_url} if last_frame_url else {}),
        }
        return

    if not model_result.content or model_result.content == "生成任务处理中":
        raise AppException("视频生成未返回有效结果", code=50231, status_code=502)

    last_frame_url = _first_generated_last_frame_url(model_result.extra)
    result_urls = extract_result_urls(model_result.content)
    history = await create_project_generated_asset_history(
        db,
        task_record=task_record,
        target_type="storyboard",
        target_id=storyboard_id,
        media_type="video",
        result_urls=result_urls or [model_result.content],
        result_url=result_urls[0] if result_urls else model_result.content,
        last_frame_url=last_frame_url,
        chapter_id=storyboard.chapter_id,
        generation_mode=(task_record.extra or {}).get("generation_mode"),
        extra={
            "storyboard_title": storyboard.title,
            "shot_number": storyboard.shot_number,
            "resolution": (task_record.extra or {}).get("resolution"),
            "return_last_frame": (task_record.extra or {}).get("return_last_frame"),
            "model_result_extra": model_result.extra,
        },
    )
    storyboard.extra = {
        **(storyboard.extra or {}),
        "video_generation_status": model_result.extra.get("platform_task_status") or "success",
        "video_generation_history_id": str(history.id),
        "video_generation_task_record_id": str(task_record.id),
        "video_generation_result": model_result.content,
        "video_generation_extra": model_result.extra,
        **({"video_generation_last_frame_url": last_frame_url} if last_frame_url else {}),
    }
    storyboard.updated_at = beijing_datetime()
    task_record.status = model_result.extra.get("platform_task_status") or "success"
    task_record.result = model_result.content
    task_record.extra = {
        **(task_record.extra or {}),
        "model_result_extra": model_result.extra,
        "storyboard_video_result": model_result.content,
        **({"storyboard_video_last_frame_url": last_frame_url} if last_frame_url else {}),
        "generated_asset_history_id": str(history.id),
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
    explicit_start_frame = _explicit_reference_start_frame_url(payload) if payload.generation_mode == "reference" else ""
    if explicit_start_frame:
        urls.append(explicit_start_frame)
    urls.extend(payload.uploaded_images or [])
    urls.extend(await _asset_reference_images(db, ProjectCharacter, project_id, user_id, payload.character_ids))
    urls.extend(await _asset_reference_images(db, ProjectScene, project_id, user_id, payload.scene_ids))
    urls.extend(await _asset_reference_images(db, ProjectProp, project_id, user_id, payload.prop_ids))
    return _dedupe(urls)


def _reference_start_frame_url(payload: ProjectStoryboardVideoGenerateRequest) -> str:
    if payload.generation_mode != "reference":
        return ""
    explicit_url = _explicit_reference_start_frame_url(payload)
    if explicit_url:
        return explicit_url
    uploaded_url = _first_url(payload.uploaded_images)
    if uploaded_url:
        return uploaded_url
    return ""


def _explicit_reference_start_frame_url(payload: ProjectStoryboardVideoGenerateRequest) -> str:
    extra = payload.extra or {}
    for value in (
        payload.reference_start_frame_url,
        extra.get("reference_start_frame_url"),
        extra.get("reference_first_frame_url"),
        extra.get("start_frame_reference_url"),
        payload.first_frame_url if payload.generation_mode == "reference" else None,
    ):
        cleaned = _clean_url(value)
        if cleaned:
            return cleaned
    return ""


def _first_url(values: List[str]) -> str:
    for value in values or []:
        cleaned = _clean_url(value)
        if cleaned:
            return cleaned
    return ""


def _clean_url(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


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


def _normalize_storyboard_video_resolution(ai_model: AiModel, resolution: str) -> str:
    if ai_model.vendor == VOLCENGINE_ARK_VENDOR or is_volcengine_ark_video_model(ai_model.model_id):
        capabilities = merge_ark_video_capabilities(ai_model.model_id, ai_model.capabilities or {})
        return normalize_video_resolution(resolution, capabilities)
    return resolution


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
    storyboard: ProjectStoryboard,
    reference_images: List[str],
    resolution: str,
    reference_start_frame_url: str = "",
) -> Dict[str, Any]:
    extra = dict(payload.extra or {})
    extra["resolution"] = resolution
    extra["return_last_frame"] = payload.return_last_frame
    if reference_start_frame_url:
        extra["reference_start_frame_url"] = reference_start_frame_url
    extra.setdefault("aspect_ratio", project.generation_ratio)
    extra.setdefault("ratio", project.generation_ratio)
    explicit_duration_seconds = _explicit_duration_seconds(extra)
    suggested_duration_seconds = _storyboard_duration_seconds(storyboard)
    if explicit_duration_seconds:
        _set_video_duration_extra(extra, explicit_duration_seconds, "request_extra")
    elif suggested_duration_seconds:
        _set_video_duration_extra(extra, suggested_duration_seconds, "storyboard_duration_suggestion")
        extra["duration_suggestion"] = storyboard.duration_suggestion
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


def _storyboard_duration_seconds(storyboard: ProjectStoryboard) -> Optional[int]:
    return _parse_duration_suggestion_seconds(storyboard.duration_suggestion)


def _parse_duration_suggestion_seconds(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    return _parse_duration_value_seconds(value)


def _explicit_duration_seconds(extra: Dict[str, Any]) -> Optional[int]:
    for key in _duration_extra_keys():
        seconds = _parse_duration_value_seconds(extra.get(key))
        if seconds:
            return seconds
    return None


def _parse_duration_value_seconds(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        if value <= 0:
            return None
        return _clamp_video_duration_seconds(math.ceil(float(value)))
    normalized = _normalize_duration_text(value)
    values = [float(item) for item in re.findall(r"\d+(?:\.\d+)?", normalized)]
    values.extend(_chinese_duration_numbers(normalized))
    if not values:
        return None
    return _clamp_video_duration_seconds(math.ceil(max(values)))


def _normalize_duration_text(value: str) -> str:
    return str(value).translate(str.maketrans("０１２３４５６７８９．", "0123456789."))


def _chinese_duration_numbers(value: str) -> List[int]:
    matches = re.findall(r"[零〇一二两三四五六七八九十]{1,4}", value)
    return [number for number in (_parse_chinese_number(match) for match in matches) if number is not None]


def _parse_chinese_number(value: str) -> Optional[int]:
    digits = {
        "零": 0,
        "〇": 0,
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    if value == "十":
        return 10
    if "十" in value:
        left, _, right = value.partition("十")
        tens = digits.get(left, 1) if left else 1
        ones = digits.get(right, 0) if right else 0
        return tens * 10 + ones
    if len(value) == 1:
        return digits.get(value)
    return None


def _clamp_video_duration_seconds(seconds: int) -> int:
    return min(max(seconds, 5), 15)


def _set_video_duration_extra(extra: Dict[str, Any], seconds: int, source: str) -> None:
    extra["duration_seconds"] = seconds
    extra["duration"] = seconds
    extra["seconds"] = seconds
    extra["duration_source"] = source


def _duration_extra_keys() -> Tuple[str, ...]:
    return (
        "duration",
        "seconds",
        "generation_seconds",
        "duration_seconds",
        "video_duration_seconds",
    )


def _first_generated_last_frame_url(extra: Dict[str, Any]) -> str:
    for key in ("display_last_frame_urls", "oss_last_frame_urls"):
        value = extra.get(key)
        if isinstance(value, list):
            for item in value:
                if item:
                    return str(item)
        if value:
            return str(value)
    return ""


def _build_storyboard_video_prompt(
    project: Project,
    storyboard: ProjectStoryboard,
    custom_prompt: Optional[str],
    generation_mode: str = "reference",
    first_frame_url: Optional[str] = None,
    last_frame_url: Optional[str] = None,
    reference_start_frame_url: str = "",
) -> str:
    style_prompt = _clean_prompt_part(project.style.prompt if project.style else "")
    main_visual = _first_prompt_part(
        storyboard.video_prompt,
        storyboard.screen_execution,
        storyboard.action,
    )
    action = _first_prompt_part(storyboard.character_action, storyboard.action)
    character_expression = _clean_prompt_part(storyboard.character_expression)
    scene_name = _clean_prompt_part(storyboard.scene_name)
    scene_state = _clean_prompt_part(storyboard.scene_state)
    atmosphere = _first_prompt_part(storyboard.atmosphere, storyboard.sound_effect)
    screen_execution = _clean_prompt_part(storyboard.screen_execution)

    parts = [
        _video_generation_constraint(
            generation_mode,
            bool(first_frame_url),
            bool(last_frame_url),
            bool(reference_start_frame_url),
        )
    ]

    if generation_mode == "reference" and reference_start_frame_url:
        parts.append(
            "第一张上传参考图是上一分镜尾帧图，必须作为本分镜视频的起始画面参考；"
            "视频开场应从这张图的构图、主体位置、人物姿态、场景状态和关键道具位置继续，"
            "其余人物参考图、场景参考图、道具参考图只用于保持身份、外观和环境一致。"
        )

    if style_prompt:
        parts.append(f"整体画面风格为：{style_prompt}")

    if scene_name or scene_state:
        scene_text = f"画面发生在{scene_name}" if scene_name else "画面场景"
        if scene_state:
            scene_text += f"，场景状态为{scene_state}"
        parts.append(scene_text + "。")

    if main_visual:
        parts.append(f"视频从以下画面开始：{main_visual}")

    if screen_execution and not _same_prompt_part(screen_execution, main_visual):
        parts.append(f"画面执行：{screen_execution}")

    if action:
        parts.append(f"角色动作：{action}")

    if character_expression:
        parts.append(f"角色表情与可见状态：{character_expression}")

    camera_text = []
    if storyboard.shot_size and storyboard.shot_size.strip():
        camera_text.append(f"景别为{storyboard.shot_size.strip()}")
    if storyboard.camera_angle and storyboard.camera_angle.strip():
        camera_text.append(f"拍摄角度为{storyboard.camera_angle.strip()}")
    if storyboard.camera_movement and storyboard.camera_movement.strip():
        camera_text.append(f"运镜方式为：{storyboard.camera_movement.strip()}")
    if camera_text:
        parts.append(_as_prompt_sentence("，".join(camera_text)))

    if atmosphere:
        parts.append(f"画面氛围参考：{atmosphere}")

    if storyboard.dialogue and storyboard.dialogue.strip():
        parts.append(
            f"角色按原文台词说话：{storyboard.dialogue.strip()}。"
            "台词只作为角色说话动作、口型和表演参考，不生成字幕文字。"
        )

    if storyboard.production_focus and storyboard.production_focus.strip():
        parts.append(f"制作重点：{storyboard.production_focus.strip()}")

    if storyboard.ending_frame and storyboard.ending_frame.strip():
        parts.append(f"视频最后停留在：{storyboard.ending_frame.strip()}")

    if storyboard.negative_prompt and storyboard.negative_prompt.strip():
        parts.append(f"避免出现：{storyboard.negative_prompt.strip()}")

    if custom_prompt and custom_prompt.strip():
        parts.append(f"用户补充要求：{custom_prompt.strip()}")

    return "\n".join(parts)


def _video_generation_constraint(
    generation_mode: str,
    has_first_frame: bool = False,
    has_last_frame: bool = False,
    has_reference_start_frame: bool = False,
) -> str:
    if generation_mode == "first_last_frame":
        if has_first_frame and has_last_frame:
            return (
                "请根据已提供的首帧图和尾帧图生成一段连续镜头视频。首帧图必须作为视频起始画面，"
                "尾帧图必须作为视频结束画面；中间过程只补充自然连贯的动作、运镜和焦点变化，"
                "保持当前分镜剧情、人物、场景、道具与首尾帧一致，不新增主要人物、场景或关键道具，"
                "不改变首尾帧的核心构图、主体身份和关键道具位置。"
            )
        if has_first_frame:
            return (
                "请以已提供的首帧图作为视频起始画面生成一段连续镜头视频。后续过程根据当前分镜补充自然连贯的动作、"
                "运镜和焦点变化，保持人物、场景、道具与首帧图一致，不新增主要人物、场景或关键道具，"
                "结尾画面遵循分镜结尾要求。"
            )
        if has_last_frame:
            return (
                "请以已提供的尾帧图作为视频结束画面生成一段连续镜头视频。开场和中间过程根据当前分镜自然过渡到尾帧图，"
                "保持人物、场景、道具与尾帧图一致，不新增主要人物、场景或关键道具，不改变尾帧图的核心构图和主体身份。"
            )
        return (
            "请按首尾帧生成思路生成一段连续镜头视频，开场、运动过程和结尾画面必须与当前分镜一致，"
            "不新增主要人物、场景或关键道具。"
        )
    if generation_mode == "storyboard":
        return (
            "请根据当前分镜和故事板参考生成一段连续镜头视频，保持分镜剧情、人物、场景、道具、动作顺序和画面收束一致，"
            "不新增主要人物、场景或关键道具。"
        )
    if has_reference_start_frame:
        return (
            "请根据当前分镜和多张参考图生成一段连续镜头视频。第一张上传参考图是上一分镜尾帧图，"
            "必须作为本分镜视频开场的首帧参考；视频开始时的人物、场景、道具、构图和画面状态应承接这张图，"
            "后续动作、运镜和焦点变化再按当前分镜自然展开。其余参考图用于保持人物身份、场景外观和道具一致，"
            "不新增主要人物、场景或关键道具。"
        )
    return (
        "请根据当前分镜和参考图生成一段连续镜头视频，保持分镜剧情、人物、场景、道具和参考图一致，"
        "不新增主要人物、场景或关键道具。"
    )


def _first_prompt_part(*values: Optional[str]) -> str:
    for value in values:
        cleaned = _clean_prompt_part(value)
        if cleaned:
            return cleaned
    return ""


def _clean_prompt_part(value: Optional[str]) -> str:
    return str(value or "").strip()


def _same_prompt_part(left: str, right: str) -> bool:
    return _clean_prompt_part(left) == _clean_prompt_part(right)


def _as_prompt_sentence(value: str) -> str:
    cleaned = _clean_prompt_part(value)
    if not cleaned:
        return ""
    if cleaned.endswith(("。", "！", "？", "；", ".", "!", "?")):
        return cleaned
    return cleaned + "。"


async def _resolve_video_provider_task(model_snapshot: SimpleNamespace, model_result: ModelRunResult) -> ModelRunResult:
    task_id = model_result.extra.get("task_id")
    if not task_id:
        return model_result
    if settings.provider_task_worker_poll_max_attempts <= 0:
        model_result.extra = {
            **model_result.extra,
            "platform_task_status": "running",
            "provider_polling_deferred": True,
            "next_poll_seconds": provider_poll_interval_seconds("video"),
        }
        model_result.content = f"模型任务仍在生成中：{task_id}"
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
    latest_result.extra["next_poll_seconds"] = provider_poll_interval_seconds("video")
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
