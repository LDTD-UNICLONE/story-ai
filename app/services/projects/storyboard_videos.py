import math
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException
from app.core.media_inputs import (
    GENERIC_UPLOAD_MEDIA_KEYS,
    drop_frame_url_keys,
    extract_upload_media_type,
    extract_uploaded_media_url,
)
from app.core.timezone import beijing_datetime
from app.integrations.apimart import APIMART_VENDOR
from app.integrations.apimart_video_specs import (
    merge_video_capabilities as merge_apimart_video_capabilities,
    normalize_video_duration as normalize_apimart_video_duration,
    normalize_video_resolution as normalize_apimart_video_resolution,
)
from app.integrations.comfly_video_specs import (
    merge_video_capabilities as merge_comfly_video_capabilities,
)
from app.integrations.volcengine_ark_video_specs import (
    VOLCENGINE_ARK_VENDOR,
    is_known_video_resolution,
    is_video_resolution_supported,
    is_volcengine_ark_video_model,
    merge_video_capabilities as merge_ark_video_capabilities,
    normalize_video_resolution,
)
from app.models.ai_model import AiModel
from app.models.agent_story_bible import AgentAssetVariant
from app.models.material import Material
from app.models.project import Project
from app.models.project_asset import ProjectCharacter, ProjectProp, ProjectScene
from app.models.project_storyboard import ProjectStoryboard
from app.models.task_record import UserTaskRecord
from app.models.user import User
from app.schemas.project_storyboard import ProjectStoryboardVideoGenerateRequest
from app.services.generation.task_dispatch import (
    dispatch_tasks_best_effort,
    enqueue_task_dispatch,
)
from app.services.generation.media import persist_generated_media_to_oss
from app.services.billing.model_points import (
    calculate_submission_points_cost,
    ensure_model_minimum_balance,
    settle_video_task_points,
)
from app.services.models.configuration import (
    build_model_runtime_snapshot,
    model_request_capabilities,
)
from app.services.generation.runner import (
    run_model, validate_video_request,
)
from app.services.billing.points import consume_user_points
from app.services.projects.generated_assets import (
    record_storyboard_video_generation_success,
)
from app.services.projects.storyboards import get_project_storyboard_or_404
from app.services.seedance_images import reviewed_video_extra
from app.services.generation.provider_polling import defer_provider_task_result
from app.services.projects.queries import get_owned_enabled_project_with_style_or_404
from app.services.generation.task_execution import lock_active_task
from app.services.generation.task_records import create_user_task_record
from app.services.generation.provider_state import (
    extract_provider_task_id,
    record_provider_task_state,
)


MODE_TO_PROVIDER_MODE = {
    "text_to_video": "text_to_video",
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
    *,
    agent_context: Optional[Dict[str, object]] = None,
) -> Tuple[UserTaskRecord, int]:
    storyboard = await get_project_storyboard_or_404(
        db, project_id, chapter_id, storyboard_id, user.id
    )
    project = await get_owned_enabled_project_with_style_or_404(db, project_id, user.id)
    ai_model = await _get_enabled_video_model_or_404(db, payload.ai_model_id)
    resolution = _normalize_storyboard_video_resolution(ai_model, payload.resolution)
    previous_ending_frame = await _previous_storyboard_ending_frame(
        db, project_id, chapter_id, storyboard, user.id
    )

    prepared_reference_images = (
        list(agent_context.get("agent_reference_images") or [])
        if agent_context and "agent_reference_images" in agent_context
        else None
    )
    reference_images = (
        prepared_reference_images
        if prepared_reference_images is not None
        else await _collect_reference_images(db, project_id, user.id, payload, storyboard)
    )
    if payload.generation_mode == "storyboard":
        if not _clean_prompt_part(storyboard.video_prompt):
            raise AppException(
                "请先生成故事板提示词，再使用故事版生成视频", code=40033, status_code=400
            )
        if not reference_images:
            raise AppException(
                "请先生成故事版图像后再使用故事版生成视频", code=40034, status_code=400
            )
    reference_images, dropped_reference_images = _limit_reference_images_for_model(
        ai_model, payload, reference_images
    )
    if prepared_reference_images is not None and dropped_reference_images:
        raise AppException(
            f"当前分镜组需要引用 {len(prepared_reference_images)} 张图片，"
            f"但所选模型最多支持 {len(reference_images)} 张",
            code=40037,
            status_code=400,
            data={
                "required_count": len(prepared_reference_images),
                "max_count": len(reference_images),
                "reference_manifest": list(
                    (agent_context or {}).get("agent_reference_manifest") or []
                ),
            },
        )
    model_extra = _build_storyboard_video_extra(
        project,
        ai_model,
        payload,
        storyboard,
        reference_images,
        resolution,
    )
    compiled_agent_prompt = str(
        (agent_context or {}).get("agent_compiled_prompt") or ""
    ).strip()
    prompt = compiled_agent_prompt or _build_storyboard_video_prompt(
        project,
        storyboard,
        payload.prompt,
        payload.generation_mode,
        payload.first_frame_url,
        payload.last_frame_url,
        previous_ending_frame,
        model_extra,
    )
    model_extra = await reviewed_video_extra(db, user.id, ai_model, prompt, model_extra)
    validate_video_request(ai_model, prompt, model_extra)
    await ensure_model_minimum_balance(db, user.id, ai_model)
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
            "previous_ending_frame": previous_ending_frame,
            "reference_images": reference_images,
            "dropped_reference_images": dropped_reference_images,
            "first_frame_url": payload.first_frame_url,
            "last_frame_url": payload.last_frame_url,
            "model_extra": model_extra,
            **(agent_context or {}),
        },
    )
    await db.flush()
    storyboard.extra = {
        **(storyboard.extra or {}),
        "video_generation_status": "pending",
        "video_generation_task_record_id": str(task_record.id),
        "video_generation_mode": payload.generation_mode,
        "video_generation_resolution": resolution,
        "video_generation_return_last_frame": payload.return_last_frame,
        "video_reference_asset_ids": {
            "character": [str(value) for value in payload.character_ids],
            "scene": [str(value) for value in payload.scene_ids],
            "prop": [str(value) for value in payload.prop_ids],
        },
    }
    dispatch_id = await enqueue_task_dispatch(
        db,
        task_name="tasks.project_storyboard_video.run_project_storyboard_video_generation",
        args=(str(task_record.id), str(storyboard_id)),
        queue="story_ai_video",
        message_id=task_record.id,
    )
    await db.commit()

    await dispatch_tasks_best_effort(db, [dispatch_id])
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

    model_snapshot = build_model_runtime_snapshot(ai_model)
    generation_extra = (task_record.extra or {}).get("model_extra") or {}
    model_result = await run_model(
        model_snapshot,
        "video",
        task_record.prompt,
        generation_extra,
        idempotency_key=str(task_record.id),
    )
    if extract_provider_task_id(model_result.extra):
        if not await lock_active_task(db, task_record):
            return
        record_provider_task_state(task_record, model_result.extra, ai_model.vendor)
        task_record.extra = {
            **(task_record.extra or {}),
            "provider_stage": "video_generation",
            "model_result_extra": model_result.extra,
        }
        await db.commit()
    provider_task_id = extract_provider_task_id(model_result.extra)
    if provider_task_id:
        model_result = defer_provider_task_result(model_result, provider_task_id)
    else:
        model_result = await persist_generated_media_to_oss("video", model_result)
    if not await lock_active_task(db, task_record):
        return
    await db.refresh(storyboard)
    record_provider_task_state(task_record, model_result.extra, ai_model.vendor)

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
    await record_storyboard_video_generation_success(
        db,
        task_record=task_record,
        storyboard=storyboard,
        content=model_result.content,
        result_extra=model_result.extra,
        last_frame_url=last_frame_url,
    )
    await settle_video_task_points(
        db,
        task_record,
        ai_model,
        (task_record.extra or {}).get("model_extra") or {},
        remark_prefix="分镜视频生成",
    )


async def _previous_storyboard_ending_frame(
    db: AsyncSession,
    project_id: UUID,
    chapter_id: UUID,
    storyboard: ProjectStoryboard,
    user_id: UUID,
) -> str:
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
    storyboards = list(result.scalars().all())
    current_index = next(
        (index for index, item in enumerate(storyboards) if item.id == storyboard.id), -1
    )
    if current_index <= 0:
        return ""
    return _clean_prompt_part(storyboards[current_index - 1].ending_frame)


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
    storyboard: Optional[ProjectStoryboard] = None,
) -> List[str]:
    urls: List[str] = []
    if payload.generation_mode == "text_to_video":
        return []
    if payload.generation_mode == "storyboard" and storyboard is not None:
        urls.extend(_storyboard_reference_images(storyboard))
        return await resolve_storyboard_reference_image_urls(db, _dedupe(urls))
    if payload.generation_mode == "first_last_frame":
        return []
    variant_asset_ids, variant_urls = await _agent_variant_reference_images(
        db,
        storyboard,
    )
    urls.extend(payload.uploaded_images or [])
    urls.extend(
        await _asset_reference_images(
            db,
            ProjectCharacter,
            project_id,
            user_id,
            [
                value
                for value in payload.character_ids
                if value not in variant_asset_ids["character"]
            ],
        )
    )
    urls.extend(
        await _asset_reference_images(
            db,
            ProjectScene,
            project_id,
            user_id,
            [value for value in payload.scene_ids if value not in variant_asset_ids["scene"]],
        )
    )
    urls.extend(
        await _asset_reference_images(
            db,
            ProjectProp,
            project_id,
            user_id,
            [value for value in payload.prop_ids if value not in variant_asset_ids["prop"]],
        )
    )
    urls.extend(variant_urls)
    return await resolve_storyboard_reference_image_urls(db, _dedupe(urls))


async def _agent_variant_reference_images(
    db: AsyncSession,
    storyboard: Optional[ProjectStoryboard],
) -> Tuple[Dict[str, set[UUID]], List[str]]:
    selected_asset_ids: Dict[str, set[UUID]] = {
        "character": set(),
        "scene": set(),
        "prop": set(),
    }
    if storyboard is None:
        return selected_asset_ids, []
    mapping = (storyboard.extra or {}).get("agent_asset_variant_ids") or {}
    requested: Dict[UUID, Tuple[str, UUID]] = {}
    for asset_type in selected_asset_ids:
        for raw_asset_id, raw_variant_id in (mapping.get(asset_type) or {}).items():
            try:
                requested[UUID(str(raw_variant_id))] = (
                    asset_type,
                    UUID(str(raw_asset_id)),
                )
            except (TypeError, ValueError, AttributeError):
                continue
    if not requested:
        return selected_asset_ids, []
    result = await db.execute(
        select(AgentAssetVariant).where(
            AgentAssetVariant.id.in_(requested),
            AgentAssetVariant.review_status == "ready",
            AgentAssetVariant.reference_image.is_not(None),
        )
    )
    urls = []
    for variant in result.scalars().all():
        asset_type, asset_id = requested[variant.id]
        reference_image = str(variant.reference_image or "").strip()
        if not reference_image:
            continue
        selected_asset_ids[asset_type].add(asset_id)
        urls.append(reference_image)
    return selected_asset_ids, urls


def _storyboard_reference_images(storyboard: ProjectStoryboard) -> List[str]:
    extra = storyboard.extra or {}
    urls: List[str] = []
    for key in ("image_generation_result", "storyboard_image_result"):
        url = _first_url(extra.get(key))
        if url:
            urls.append(url)
    result_urls = extra.get("image_generation_result_urls")
    if isinstance(result_urls, list):
        urls.extend(_first_url(item) for item in result_urls)
    return _dedupe([url for url in urls if url])


async def resolve_storyboard_reference_image_urls(
    db: AsyncSession,
    urls: List[str],
) -> List[str]:
    material_ids = [_material_id_from_image_url(url) for url in urls]
    material_ids = [material_id for material_id in material_ids if material_id is not None]
    material_url_map: Dict[UUID, str] = {}
    if material_ids:
        result = await db.execute(
            select(Material.id, Material.image_url).where(
                Material.id.in_(material_ids),
                Material.is_enabled.is_(True),
                Material.image_url.is_not(None),
            )
        )
        material_url_map = {row[0]: row[1] for row in result.all() if row[1]}

    resolved: List[str] = []
    for url in urls:
        material_id = _material_id_from_image_url(url)
        resolved.append(material_url_map.get(material_id, url) if material_id else url)
    return resolved


def _material_id_from_image_url(value: Any) -> Optional[UUID]:
    url = _clean_url(value)
    if not url:
        return None
    try:
        path = urlsplit(url).path or url
    except Exception:
        path = url
    parts = [part for part in path.split("/") if part]
    for index, part in enumerate(parts):
        if part != "materials" or index + 2 >= len(parts):
            continue
        if parts[index + 2] != "image":
            continue
        try:
            return UUID(parts[index + 1])
        except ValueError:
            return None
    return None


def _clean_url(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _first_url(value: Any) -> str:
    if isinstance(value, list):
        for item in value:
            url = _first_url(item)
            if url:
                return url
        return ""
    for item in str(value or "").split(","):
        url = item.strip()
        if url.startswith(("http://", "https://")):
            return url
    return ""


def _limit_reference_images_for_model(
    ai_model: AiModel,
    payload: ProjectStoryboardVideoGenerateRequest,
    reference_images: List[str],
) -> Tuple[List[str], List[str]]:
    if payload.generation_mode == "storyboard":
        return reference_images[:1], reference_images[1:]

    max_images = storyboard_video_image_limit(ai_model)
    if max_images is None:
        return reference_images, []
    if max_images <= 0:
        return [], reference_images

    reserved_count = 0
    if payload.generation_mode == "first_last_frame":
        reserved_count += 1 if payload.first_frame_url else 0
        reserved_count += 1 if payload.last_frame_url else 0

    allowed_reference_count = max(0, max_images - reserved_count)
    if len(reference_images) <= allowed_reference_count:
        return reference_images, []
    return reference_images[:allowed_reference_count], reference_images[allowed_reference_count:]


def storyboard_video_image_limit(ai_model: AiModel) -> Optional[int]:
    saved = model_request_capabilities(ai_model)
    if ai_model.vendor == VOLCENGINE_ARK_VENDOR or is_volcengine_ark_video_model(ai_model.model_id):
        capabilities = merge_ark_video_capabilities(ai_model.model_id, saved)
    elif ai_model.vendor == APIMART_VENDOR:
        capabilities = merge_apimart_video_capabilities(ai_model.model_id, saved)
    else:
        capabilities = merge_comfly_video_capabilities(ai_model.model_id, saved)
    media_limits = (capabilities or {}).get("media_limits") or {}
    raw_limit = media_limits.get("images")
    if isinstance(raw_limit, int) and raw_limit >= 0:
        return raw_limit
    if ai_model.vendor == VOLCENGINE_ARK_VENDOR or is_volcengine_ark_video_model(ai_model.model_id):
        return 9
    return None


def _normalize_storyboard_video_resolution(ai_model: AiModel, resolution: str) -> str:
    if ai_model.vendor == APIMART_VENDOR:
        return normalize_apimart_video_resolution(ai_model.model_id, resolution)
    if ai_model.vendor == VOLCENGINE_ARK_VENDOR or is_volcengine_ark_video_model(ai_model.model_id):
        capabilities = merge_ark_video_capabilities(
            ai_model.model_id,
            model_request_capabilities(ai_model),
        )
        if not is_known_video_resolution(resolution) or not is_video_resolution_supported(
            resolution, capabilities
        ):
            raise AppException(
                "当前火山方舟视频模型不支持该 resolution 参数", code=40012, status_code=400
            )
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
) -> Dict[str, Any]:
    extra = dict(payload.extra or {})
    if payload.generation_mode in {"storyboard", "text_to_video"}:
        _clear_video_reference_media_extra(extra)
    extra["resolution"] = resolution
    extra["return_last_frame"] = payload.return_last_frame
    extra.setdefault("aspect_ratio", project.generation_ratio)
    extra.setdefault("ratio", project.generation_ratio)
    duration_capability = _video_duration_capability(ai_model)
    explicit_duration_seconds = _explicit_duration_seconds(
        extra,
        ai_model,
        payload.generation_mode,
    )
    suggested_duration_seconds = (
        _storyboard_duration_seconds(
            storyboard,
            duration_capability["min"],
            duration_capability["max"],
        )
        if explicit_duration_seconds is None and duration_capability["controllable"]
        else None
    )
    if suggested_duration_seconds is not None and ai_model.vendor == APIMART_VENDOR:
        suggested_duration_seconds = normalize_apimart_video_duration(
            ai_model.model_id,
            suggested_duration_seconds,
            mode=payload.generation_mode,
        )
    if explicit_duration_seconds is not None:
        _set_video_duration_extra(extra, explicit_duration_seconds, "request_extra")
    elif suggested_duration_seconds is not None:
        _set_video_duration_extra(
            extra, suggested_duration_seconds, "storyboard_duration_suggestion"
        )
        extra["duration_suggestion"] = storyboard.duration_suggestion
    extra["video_mode"] = MODE_TO_PROVIDER_MODE[payload.generation_mode]
    extra["capability"] = MODE_TO_PROVIDER_MODE[payload.generation_mode]
    extra["generation_mode"] = payload.generation_mode
    if payload.generation_mode == "text_to_video":
        drop_frame_url_keys(extra)
        return extra
    if (
        payload.generation_mode == "reference"
        and not reference_images
        and not _has_reference_image_or_video_extra(extra)
    ):
        raise AppException("参考生成需要至少传入参考图片或参考视频", code=40012, status_code=400)
    if reference_images and payload.generation_mode != "first_last_frame":
        extra["images"] = reference_images
        if ai_model.vendor != VOLCENGINE_ARK_VENDOR and not is_volcengine_ark_video_model(
            ai_model.model_id
        ):
            extra["image_urls"] = reference_images
    if payload.generation_mode == "first_last_frame":
        if not payload.first_frame_url:
            raise AppException("首尾帧生成需要传入 first_frame_url", code=40012, status_code=400)
        _clear_video_reference_media_extra(extra)
        extra["first_frame_url"] = payload.first_frame_url
        media_items = [
            {
                "type": "image_url",
                "image_url": {"url": payload.first_frame_url},
                "role": "first_frame",
            }
        ]
        if payload.last_frame_url:
            extra["last_frame_url"] = payload.last_frame_url
            media_items.append(
                {
                    "type": "image_url",
                    "image_url": {"url": payload.last_frame_url},
                    "role": "last_frame",
                }
            )
        extra["media_items"] = media_items
    return extra


def _clear_video_reference_media_extra(extra: Dict[str, Any]) -> None:
    for key in (
        "audio",
        "audio_url",
        "audio_urls",
        "audios",
        "content",
        "file",
        "file_url",
        "file_urls",
        "fileUrl",
        "fileUrls",
        "files",
        "image",
        "images",
        "image_url",
        "image_urls",
        "imageUrl",
        "imageUrls",
        "media",
        "media_items",
        "referenceImage",
        "referenceImageUrl",
        "referenceImageUrls",
        "referenceImages",
        "reference_image",
        "reference_images",
        "reference_image_url",
        "reference_image_urls",
        "referenceVideo",
        "referenceVideoUrl",
        "referenceVideoUrls",
        "referenceVideos",
        "reference_video",
        "reference_video_url",
        "reference_video_urls",
        "reference_videos",
        "upload",
        "uploaded_file",
        "uploaded_files",
        "uploadedFile",
        "uploadedFiles",
        "uploaded_images",
        "uploadedImages",
        "video",
        "video_url",
        "video_urls",
        "videoUrl",
        "videoUrls",
        "videos",
        "uploads",
        "attachment",
        "attachment_url",
        "attachment_urls",
        "attachmentUrl",
        "attachmentUrls",
        "attachments",
    ):
        extra.pop(key, None)


def _has_reference_image_or_video_extra(extra: Dict[str, Any]) -> bool:
    for key in (
        "image",
        "image_url",
        "image_urls",
        "imageUrl",
        "imageUrls",
        "images",
        "reference_image",
        "reference_image_url",
        "reference_image_urls",
        "reference_images",
        "referenceImage",
        "referenceImageUrl",
        "referenceImageUrls",
        "referenceImages",
        "uploaded_images",
        "uploadedImages",
        "video",
        "video_url",
        "video_urls",
        "videoUrl",
        "videoUrls",
        "videos",
        "reference_video",
        "reference_video_url",
        "reference_video_urls",
        "reference_videos",
        "referenceVideo",
        "referenceVideoUrl",
        "referenceVideoUrls",
        "referenceVideos",
    ):
        if _has_extra_value(extra.get(key)):
            return True
    for key in GENERIC_UPLOAD_MEDIA_KEYS:
        for item in _as_list(extra.get(key)):
            if _uploaded_media_type(item) in {"image", "video"}:
                return True
    for key in ("media", "media_items", "content"):
        for item in _as_list(extra.get(key)):
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "").strip()
            role = str(item.get("role") or "").strip().replace("-", "_")
            if item_type == "video_url" or role in {
                "reference_video",
                "video",
                "ref_video",
                "referenceVideo",
            }:
                return True
            if item_type == "image_url" and role in {
                "",
                "reference_image",
                "reference",
                "image",
                "ref_image",
                "referenceImage",
            }:
                return True
    return False


def _uploaded_media_type(value: Any) -> str:
    raw_type = ""
    if isinstance(value, dict):
        raw_type = extract_upload_media_type(value)
    if raw_type.startswith("image/") or raw_type in {"image", "img", "image_url"}:
        return "image"
    if raw_type.startswith("video/") or raw_type in {"video", "video_url"}:
        return "video"

    url = extract_uploaded_media_url(value).lower().split("?", 1)[0]
    if url.endswith((".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".gif", ".heic", ".heif")):
        return "image"
    if url.endswith((".mp4", ".mov", ".webm", ".m4v", ".avi", ".mkv")):
        return "video"
    return ""


def _has_extra_value(value: Any) -> bool:
    if value in (None, "", []):
        return False
    if isinstance(value, dict):
        return any(_has_extra_value(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_extra_value(item) for item in value)
    return True


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _storyboard_duration_seconds(
    storyboard: ProjectStoryboard,
    min_seconds: int = 5,
    max_seconds: int = 15,
) -> Optional[int]:
    return _parse_duration_suggestion_seconds(
        storyboard.duration_suggestion,
        min_seconds,
        max_seconds,
    )


def _parse_duration_suggestion_seconds(
    value: Optional[str],
    min_seconds: int = 5,
    max_seconds: int = 15,
) -> Optional[int]:
    if not value:
        return None
    return _parse_duration_value_seconds(value, min_seconds, max_seconds)


def _explicit_duration_seconds(
    extra: Dict[str, Any],
    ai_model: AiModel,
    generation_mode: str,
) -> Any:
    for key in _duration_extra_keys():
        value = extra.get(key)
        if value in (None, ""):
            continue
        if ai_model.vendor == APIMART_VENDOR:
            return normalize_apimart_video_duration(
                ai_model.model_id,
                value,
                mode=generation_mode,
            )
        # Explicit input must reach the provider validator unchanged. Only a
        # storyboard's suggested duration is parsed and clamped automatically.
        return value
    return None


def _parse_duration_value_seconds(
    value: Any,
    min_seconds: int = 5,
    max_seconds: int = 15,
) -> Optional[int]:
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        if value <= 0:
            return None
        return _clamp_video_duration_seconds(
            math.ceil(float(value)), min_seconds, max_seconds
        )
    normalized = _normalize_duration_text(value)
    values = [float(item) for item in re.findall(r"\d+(?:\.\d+)?", normalized)]
    values.extend(_chinese_duration_numbers(normalized))
    if not values:
        return None
    return _clamp_video_duration_seconds(
        math.ceil(max(values)), min_seconds, max_seconds
    )


def _normalize_duration_text(value: str) -> str:
    return str(value).translate(str.maketrans("０１２３４５６７８９．", "0123456789."))


def _chinese_duration_numbers(value: str) -> List[int]:
    matches = re.findall(r"[零〇一二两三四五六七八九十]{1,4}", value)
    return [
        number
        for number in (_parse_chinese_number(match) for match in matches)
        if number is not None
    ]


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


def _clamp_video_duration_seconds(
    seconds: int,
    min_seconds: int = 5,
    max_seconds: int = 15,
) -> int:
    return min(max(seconds, min_seconds), max_seconds)


def _set_video_duration_extra(extra: Dict[str, Any], seconds: Any, source: str) -> None:
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


def _video_min_duration_seconds(ai_model: AiModel) -> int:
    if ai_model.vendor == VOLCENGINE_ARK_VENDOR or is_volcengine_ark_video_model(ai_model.model_id):
        return 4
    return 5


def _video_duration_capability(ai_model: AiModel) -> Dict[str, Any]:
    if ai_model.vendor == APIMART_VENDOR:
        duration = merge_apimart_video_capabilities(
            ai_model.model_id,
            model_request_capabilities(ai_model),
        )["duration"]
        return {
            "min": int(duration["min"]),
            "max": int(duration["max"]),
            "controllable": bool(duration.get("controllable", True)),
        }
    return {
        "min": _video_min_duration_seconds(ai_model),
        "max": 15,
        "controllable": True,
    }


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
    previous_ending_frame: str = "",
    model_extra: Optional[Dict[str, Any]] = None,
) -> str:
    if generation_mode == "storyboard":
        return _build_storyboard_reference_video_prompt(
            project, storyboard, custom_prompt, model_extra
        )

    style_prompt = _clean_prompt_part(project.style.prompt if project.style else "")
    opening_visual = _first_prompt_part(
        previous_ending_frame, storyboard.screen_execution, storyboard.action
    )
    action = _first_prompt_part(storyboard.character_action, storyboard.action)
    character_expression = _clean_prompt_part(storyboard.character_expression)
    scene_name = _clean_prompt_part(storyboard.scene_name)
    scene_state = _clean_prompt_part(storyboard.scene_state)
    atmosphere = _clean_prompt_part(storyboard.atmosphere)
    sound_effect = _clean_prompt_part(storyboard.sound_effect)
    screen_execution = _clean_prompt_part(storyboard.screen_execution)
    duration_suggestion = _clean_prompt_part(storyboard.duration_suggestion)

    parts = [
        _video_generation_constraint(
            generation_mode,
            bool(first_frame_url),
            bool(last_frame_url),
        )
    ]

    if style_prompt:
        parts.append(f"整体画面风格为：{style_prompt}")

    if duration_suggestion:
        parts.append(
            f"视频建议时长：{duration_suggestion}。"
            "所有动作、运镜、表情、台词、声音节奏、氛围变化和结尾画面都必须在该时长内自然完成，"
            "画面中不得出现任何时间文字。"
        )

    if scene_name or scene_state:
        scene_text = f"画面发生在{scene_name}" if scene_name else "画面发生在当前分镜场景"
        if scene_state:
            scene_text += f"，场景状态为{scene_state}"
        parts.append(scene_text + "。")

    if opening_visual:
        parts.append(f"视频开场画面：{opening_visual}")

    if screen_execution:
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

    if sound_effect:
        sound_effect_sentence = _as_prompt_sentence(f"声音与节奏参考：{sound_effect}")
        parts.append(
            sound_effect_sentence
            + "声音与节奏只作为动作节奏和氛围参考，不生成字幕文字、声音文字或可视化音效文字。"
        )

    if storyboard.dialogue and storyboard.dialogue.strip():
        dialogue_sentence = _as_prompt_sentence(
            f"角色按原文台词进行说话表演：{storyboard.dialogue.strip()}"
        )
        parts.append(
            dialogue_sentence + "台词只用于嘴型、停顿、视线和表演节奏参考，"
            "画面中不得出现字幕、气泡文字、台词文字或任何屏幕文字。"
        )

    if storyboard.production_focus and storyboard.production_focus.strip():
        parts.append(f"制作重点：{storyboard.production_focus.strip()}")

    if storyboard.ending_frame and storyboard.ending_frame.strip():
        parts.append(f"视频最后停留在：{storyboard.ending_frame.strip()}")

    if storyboard.negative_prompt and storyboard.negative_prompt.strip():
        parts.append(f"避免出现：{storyboard.negative_prompt.strip()}")

    if custom_prompt and custom_prompt.strip():
        custom_prompt_sentence = _as_prompt_sentence(f"用户补充要求：{custom_prompt.strip()}")
        parts.append(
            custom_prompt_sentence
            + "用户补充要求只能补充当前镜头的表现方式，不得覆盖当前分镜剧情、人物资产、场景资产、"
            "道具资产、参考图一致性、视频建议时长、制作重点和负面规避要求。"
        )

    return "\n\n".join(parts)


def _build_storyboard_reference_video_prompt(
    project: Project,
    storyboard: ProjectStoryboard,
    custom_prompt: Optional[str],
    model_extra: Optional[Dict[str, Any]] = None,
) -> str:
    agent_prompt = _clean_prompt_part(
        (storyboard.extra or {}).get("agent_storyboard_prompt")
    )
    if agent_prompt:
        parts = [agent_prompt]
        variant_text = _agent_variant_context_text(storyboard)
        if variant_text:
            parts.append(f"当前选定资产变体：{variant_text}")
        if custom_prompt and custom_prompt.strip():
            parts.append(f"用户补充要求：{custom_prompt.strip()}")
        return "\n".join(parts)

    style_prompt = _clean_prompt_part(project.style.prompt if project.style else "")
    duration_suggestion = _video_prompt_duration_text(storyboard, model_extra or {})
    video_prompt = _clean_prompt_part(storyboard.video_prompt)
    dialogue = _clean_prompt_part(storyboard.dialogue)
    user_prompt = _clean_prompt_part(custom_prompt)
    negative_prompt = _clean_prompt_part(storyboard.negative_prompt)

    return "\n".join(
        [
            "请根据当前分镜的故事版参考图、连续视频画面提示词、整体画面风格和用户补充要求，生成一段连续视频。",
            "当前视频必须以故事版参考图为主要视觉依据，保持故事版参考图中的人物形象、服装、场景空间、道具位置、构图关系、画面氛围和镜头顺序一致。",
            f"整体画面风格：{style_prompt}",
            f"视频建议总时长：{duration_suggestion}",
            f"连续视频画面提示词：{video_prompt}",
            f"原文台词参考：{dialogue}",
            "生成要求：",
            "1. 必须参考故事版图像生成视频，故事版图像中的人物、服装、场景、道具、构图和空间关系优先保持一致。",
            "2. 严格按照 video_prompt 中的“通用要求、镜头一、镜头二、镜头三、最后停留画面”进行视频生成。",
            "3. 每个镜头的秒数必须与 video_prompt 中括号秒数一致。",
            f"4. 所有镜头总时长必须等于 {duration_suggestion}。",
            "5. 镜头之间必须保持人物身份、服装、发型、场景空间、道具位置、道具状态、动作方向和视线方向连续。",
            "6. 每个镜头只表现对应故事版图像的动态画面，不新增当前分镜之外的新剧情。",
            "7. 可以根据 video_prompt 让故事版图像中的人物产生自然动作、表情变化、视线变化、轻微运镜和焦点变化。",
            "8. 不得改变故事版图像中的主体身份、人物服装、主要场景、关键道具和主要构图关系。",
            "9. 台词只用于人物嘴型、停顿、视线和表演节奏参考，不生成字幕、气泡文字或台词文字。",
            "10. 画面中不得出现字幕、气泡文字、台词文字、屏幕文字、水印、标志或界面元素。",
            "11. 视频最后必须停留在 video_prompt 中的“最后停留画面”。",
            f"用户补充要求：{user_prompt}",
            "用户补充要求只能补充当前视频表现方式，例如动作强度、节奏、氛围、镜头运动或画面质感；不得覆盖当前分镜剧情、故事版参考图、人物资产、场景资产、道具资产、视频建议总时长、镜头连续性和负面规避要求。",
            f"负面规避：{negative_prompt}",
        ]
    )


def _agent_variant_context_text(storyboard: ProjectStoryboard) -> str:
    items = (storyboard.extra or {}).get("agent_asset_variant_context") or []
    return "；".join(
        f"{item.get('name')}（{item.get('description')}，触发：{item.get('trigger_reason')}）"
        for item in items
        if isinstance(item, dict) and item.get("name")
    )


def _video_prompt_duration_text(storyboard: ProjectStoryboard, model_extra: Dict[str, Any]) -> str:
    raw_seconds = model_extra.get("duration_seconds") or model_extra.get("duration")
    seconds = int(raw_seconds) if isinstance(raw_seconds, int) and raw_seconds > 0 else None
    if seconds:
        return f"{seconds}秒"
    return _clean_prompt_part(storyboard.duration_suggestion)


def _video_generation_constraint(
    generation_mode: str,
    has_first_frame: bool = False,
    has_last_frame: bool = False,
) -> str:
    if generation_mode == "text_to_video":
        return (
            "请根据当前分镜文本生成一段连续镜头视频。画面必须遵循当前分镜剧情、人物、场景、道具、"
            "动作、镜头语言、氛围和结尾要求；不依赖任何参考图、参考视频或参考音频，"
            "不新增主要人物、场景或关键道具。"
        )
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


def _as_prompt_sentence(value: str) -> str:
    cleaned = _clean_prompt_part(value)
    if not cleaned:
        return ""
    if cleaned.endswith(("。", "！", "？", "；", ".", "!", "?")):
        return cleaned
    return cleaned + "。"


def _dedupe(values: List[str]) -> List[str]:
    items: List[str] = []
    seen = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            items.append(value)
    return items
