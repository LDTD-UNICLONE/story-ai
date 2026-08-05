import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.exceptions import AppException
from app.core.timezone import beijing_datetime
from app.integrations import comfly
from app.models.ai_model import AiModel
from app.models.agent_story_bible import AgentAssetVariant
from app.models.material import Material
from app.models.project import Project
from app.models.project_asset import ProjectCharacter, ProjectProp, ProjectScene
from app.models.project_storyboard import ProjectStoryboard
from app.models.task_record import UserTaskRecord
from app.models.user import User
from app.schemas.project_storyboard import ProjectStoryboardImageGenerateRequest
from app.services.generated_media import persist_generated_media_to_oss
from app.services.model_points import calculate_submission_points_cost
from app.services.model_runner import ModelRunResult, query_model_task, run_model
from app.services.points import change_user_points, consume_user_points
from app.services.project_generated_assets import (
    create_project_generated_asset_history,
    extract_result_urls,
)
from app.services.project_storyboards import get_project_storyboard_or_404
from app.services.projects import get_project_or_404
from app.services.provider_polling import provider_poll_interval_seconds
from app.services.task_records import (
    create_user_task_record,
    record_provider_task_state,
    refresh_task_record_interrupted,
)


DEFAULT_STORYBOARD_IMAGE_MODEL_ID = "gpt-image-2"


async def submit_storyboard_image_generation(
    db: AsyncSession,
    project_id: UUID,
    chapter_id: UUID,
    storyboard_id: UUID,
    user: User,
    payload: ProjectStoryboardImageGenerateRequest,
    *,
    agent_context: Optional[Dict[str, object]] = None,
) -> Tuple[UserTaskRecord, int]:
    await get_project_or_404(db, project_id, user.id)
    project = await _get_project_with_style_or_404(db, project_id, user.id)
    storyboard = await get_project_storyboard_or_404(
        db, project_id, chapter_id, storyboard_id, user.id
    )
    ai_model = await _get_storyboard_image_model_or_404(db, payload.ai_model_id)

    reference_assets = await _collect_reference_assets(db, project_id, user.id, storyboard, payload)
    reference_images = await _resolve_reference_image_urls(
        db,
        _dedupe(
            [
                *(payload.uploaded_images or []),
                *[
                    asset["reference_image"]
                    for asset in reference_assets
                    if asset.get("reference_image")
                ],
            ]
        ),
    )
    prompt = _build_storyboard_image_prompt(project, storyboard, reference_assets, payload.prompt)
    model_extra = _build_storyboard_image_extra(payload, reference_images)
    _validate_comfly_storyboard_image_request(ai_model, prompt, model_extra)
    points_cost = calculate_submission_points_cost(ai_model, "image", model_extra)

    points_transaction = None
    if points_cost > 0:
        points_transaction = await consume_user_points(
            db,
            user_id=user.id,
            amount=points_cost,
            remark=f"分镜故事板图像生成：{storyboard.title}",
            auto_commit=False,
        )

    task_record = await create_user_task_record(
        db,
        user_id=user.id,
        ai_model_id=ai_model.id,
        points_transaction_id=points_transaction.id if points_transaction else None,
        business_type="project",
        business_id=project_id,
        generation_type="storyboard_image",
        status="pending",
        title=f"分镜故事板图像生成：{storyboard.title}",
        prompt=prompt,
        result=None,
        points_cost=points_cost,
        extra={
            "project_id": str(project_id),
            "chapter_id": str(chapter_id),
            "storyboard_id": str(storyboard_id),
            "aspect_ratio": payload.aspect_ratio,
            "generation_ratio": payload.aspect_ratio,
            "reference_images": reference_images,
            "reference_assets": reference_assets,
            "model_extra": model_extra,
            **(agent_context or {}),
        },
    )
    await db.flush()
    storyboard.extra = {
        **(storyboard.extra or {}),
        "image_generation_status": "pending",
        "image_generation_task_record_id": str(task_record.id),
        "image_generation_aspect_ratio": payload.aspect_ratio,
        "image_reference_asset_ids": _reference_asset_ids(reference_assets),
    }
    await db.commit()

    try:
        from app.tasks.project_storyboard_image import run_project_storyboard_image_generation

        run_project_storyboard_image_generation.apply_async(
            args=(str(task_record.id), str(storyboard_id)),
            queue="story_ai_image",
            routing_key="story_ai_image",
        )
    except Exception:
        await _mark_storyboard_image_enqueue_failed(db, task_record, storyboard)
    return task_record, points_cost


async def run_storyboard_image_generation_in_worker(
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
            AiModel.model_type == "image",
            AiModel.is_enabled.is_(True),
        )
    )
    ai_model = result.scalar_one_or_none()
    if ai_model is None:
        raise AppException("图像模型不存在或已禁用", code=40404, status_code=404)

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
        "image",
        task_record.prompt,
        (task_record.extra or {}).get("model_extra") or {},
        idempotency_key=str(task_record.id),
    )
    if record_provider_task_state(task_record, model_result.extra, ai_model.vendor):
        task_record.extra = {
            **(task_record.extra or {}),
            "model_result_extra": model_result.extra,
        }
        await db.commit()
    model_result = await _resolve_image_provider_task(model_snapshot, model_result)
    model_result = await persist_generated_media_to_oss("image", model_result)
    if await refresh_task_record_interrupted(db, task_record):
        return
    record_provider_task_state(task_record, model_result.extra, ai_model.vendor)

    if model_result.extra.get("platform_task_status") == "running":
        storyboard.extra = {
            **(storyboard.extra or {}),
            "image_generation_status": "running",
            "image_generation_task_record_id": str(task_record.id),
            "image_generation_result": model_result.content,
            "image_generation_extra": model_result.extra,
        }
        storyboard.updated_at = beijing_datetime()
        task_record.status = "running"
        task_record.result = model_result.content
        task_record.extra = {
            **(task_record.extra or {}),
            "model_result_extra": model_result.extra,
            "storyboard_image_result": model_result.content,
        }
        return

    image_url = _first_result_url(model_result.content)
    if not image_url:
        raise AppException("图像生成未返回有效结果", code=50231, status_code=502)

    history = await create_project_generated_asset_history(
        db,
        task_record=task_record,
        target_type="storyboard",
        target_id=storyboard_id,
        media_type="image",
        result_urls=extract_result_urls(model_result.content) or [image_url],
        result_url=image_url,
        chapter_id=storyboard.chapter_id,
        generation_mode="storyboard_image",
        extra={
            "storyboard_title": storyboard.title,
            "shot_number": storyboard.shot_number,
            "aspect_ratio": (task_record.extra or {}).get("aspect_ratio"),
            "reference_images": (task_record.extra or {}).get("reference_images"),
            "model_result_extra": model_result.extra,
        },
    )
    storyboard.extra = {
        **(storyboard.extra or {}),
        "image_generation_status": "success",
        "image_generation_history_id": str(history.id),
        "image_generation_task_record_id": str(task_record.id),
        "image_generation_result": image_url,
        "image_generation_result_urls": extract_result_urls(model_result.content) or [image_url],
        "image_generation_extra": model_result.extra,
    }
    storyboard.updated_at = beijing_datetime()
    task_record.status = "success"
    task_record.result = image_url
    task_record.extra = {
        **(task_record.extra or {}),
        "model_result_extra": model_result.extra,
        "oss_image_url": image_url,
        "storyboard_image_result": image_url,
        "generated_asset_history_id": str(history.id),
    }


async def _get_project_with_style_or_404(
    db: AsyncSession, project_id: UUID, user_id: UUID
) -> Project:
    result = await db.execute(
        select(Project)
        .options(selectinload(Project.style))
        .where(Project.id == project_id, Project.user_id == user_id, Project.is_enabled.is_(True))
    )
    project = result.scalar_one_or_none()
    if project is None:
        raise AppException("项目不存在", code=40407, status_code=404)
    return project


async def _get_storyboard_image_model_or_404(
    db: AsyncSession,
    ai_model_id: Optional[UUID] = None,
) -> AiModel:
    identity_condition = (
        AiModel.id == ai_model_id
        if ai_model_id is not None
        else AiModel.model_id == DEFAULT_STORYBOARD_IMAGE_MODEL_ID
    )
    result = await db.execute(
        select(AiModel)
        .where(
            identity_condition,
            AiModel.model_type == "image",
            AiModel.is_enabled.is_(True),
        )
        .order_by(AiModel.created_at.desc())
        .limit(1)
    )
    ai_model = result.scalar_one_or_none()
    if ai_model is None:
        raise AppException("故事板图像模型不存在、未启用或类型不匹配", code=40404, status_code=404)
    return ai_model


def _build_storyboard_image_extra(
    payload: ProjectStoryboardImageGenerateRequest,
    reference_images: List[str],
) -> Dict[str, Any]:
    extra = dict(payload.extra or {})
    extra.setdefault("aspect_ratio", payload.aspect_ratio)
    extra.setdefault("ratio", payload.aspect_ratio)
    extra.setdefault("image_mode", "storyboard")
    extra.setdefault("capability", "generation")
    if reference_images:
        extra["images"] = reference_images
        extra["image_urls"] = reference_images
    return extra


def _validate_comfly_storyboard_image_request(
    ai_model: AiModel, prompt: str, extra: Dict[str, Any]
) -> None:
    if ai_model.vendor not in {"comfly", "模型服务"}:
        return
    comfly.validate_image_request(ai_model.model_id, prompt, extra)


def _build_storyboard_image_prompt(
    project: Project,
    storyboard: ProjectStoryboard,
    reference_assets: List[Dict[str, Any]],
    custom_prompt: Optional[str] = None,
) -> str:
    style_prompt = _clean_prompt_part(project.style.prompt if project.style else "")
    title = _clean_prompt_part(storyboard.title)
    image_prompt = _clean_prompt_part(storyboard.image_prompt)
    if not image_prompt:
        image_prompt = _first_prompt_part(
            storyboard.screen_execution, storyboard.action, storyboard.source_content
        )
    characters = _join_names(storyboard.characters)
    scene_name = _clean_prompt_part(storyboard.scene_name)
    props = _join_names(storyboard.props)
    negative_prompt = _clean_prompt_part(storyboard.negative_prompt)
    reference_text = _format_reference_assets(reference_assets)
    variant_text = _agent_variant_context_text(storyboard)

    parts = [
        "请根据当前故事版镜头和参考资产生成一张故事版图像，每一个分镜头下需要使用中文标注清楚信息，镜头时长，整体氛围，背景音乐，底部灯光，情绪，摄影机位变化等等可以根据内容多分镜头，但是要保证场景、人物等资产的真实逻辑。不能出现气泡文字，不能出现字幕等。所有的参数都是在镜头图像下面标注，每个镜头要标注清楚",
        f"整体画面风格：{style_prompt}",
        f"当前分镜标题：{title}",
        f"当前镜头图像提示词：{image_prompt}",
        f"绑定人物：{characters}",
        f"绑定场景：{scene_name}",
        f"绑定道具：{props}",
    ]
    if variant_text:
        parts.append(f"当前选定资产变体：{variant_text}")
    if reference_text:
        parts.append(f"参考资产：\n{reference_text}")
    parts.extend(
        [
            "生成要求：",
            "1. 将画面提示词中的镜头描述按顺序生成一张故事版画面。",
            "2. 保持人物、场景、道具与参考资产一致。",
            "3. 画面必须体现当前镜头的主体、动作或姿态、表情状态、景别、拍摄角度、构图重点和氛围。",
            "4. 不生成连续视频动作。",
            "5. 不新增当前分镜之外的人物、场景、道具或剧情。",
            "6. 不生成角色设定图、场景设定图、道具设定图、封面图或海报图。",
            "7. 画面中不得出现字幕、气泡文字、台词文字、屏幕文字、水印、标志或界面元素。",
            "负面规避：",
            negative_prompt,
        ]
    )
    if custom_prompt and custom_prompt.strip():
        parts.append(
            "用户补充要求："
            + custom_prompt.strip()
            + "。用户补充要求只能补充当前故事版图像的表现方式，不得覆盖当前分镜剧情、绑定资产、参考图一致性和负面规避要求。"
        )
    return "\n".join(part for part in parts if part is not None)


def _agent_variant_context_text(storyboard: ProjectStoryboard) -> str:
    items = (storyboard.extra or {}).get("agent_asset_variant_context") or []
    return "；".join(
        f"{item.get('name')}（{item.get('description')}，触发：{item.get('trigger_reason')}）"
        for item in items
        if isinstance(item, dict) and item.get("name")
    )


async def _collect_reference_assets(
    db: AsyncSession,
    project_id: UUID,
    user_id: UUID,
    storyboard: ProjectStoryboard,
    payload: ProjectStoryboardImageGenerateRequest,
) -> List[Dict[str, Any]]:
    assets: List[Dict[str, Any]] = []
    assets.extend(
        await _asset_records_by_ids_or_names(
            db,
            ProjectCharacter,
            "character",
            project_id,
            user_id,
            payload.character_ids,
            _as_name_list(storyboard.characters),
        )
    )
    assets.extend(
        await _asset_records_by_ids_or_names(
            db,
            ProjectScene,
            "scene",
            project_id,
            user_id,
            payload.scene_ids,
            [storyboard.scene_name] if storyboard.scene_name else [],
        )
    )
    assets.extend(
        await _asset_records_by_ids_or_names(
            db,
            ProjectProp,
            "prop",
            project_id,
            user_id,
            payload.prop_ids,
            _as_name_list(storyboard.props),
        )
    )
    return await _apply_agent_variant_reference_images(db, storyboard, assets)


async def _apply_agent_variant_reference_images(
    db: AsyncSession,
    storyboard: ProjectStoryboard,
    assets: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    mapping = (storyboard.extra or {}).get("agent_asset_variant_ids") or {}
    requested: Dict[UUID, Tuple[str, str]] = {}
    for asset_type in ("character", "scene", "prop"):
        for asset_id, raw_variant_id in (mapping.get(asset_type) or {}).items():
            try:
                requested[UUID(str(raw_variant_id))] = (asset_type, str(asset_id))
            except ValueError:
                continue
    if not requested:
        return assets
    result = await db.execute(
        select(AgentAssetVariant).where(
            AgentAssetVariant.id.in_(requested),
            AgentAssetVariant.review_status == "ready",
        )
    )
    variants = {variant.id: variant for variant in result.scalars().all()}
    variant_by_asset = {
        key: variants[variant_id]
        for variant_id, key in requested.items()
        if variant_id in variants and variants[variant_id].reference_image
    }
    return [
        {
            **asset,
            "name": variant.canonical_name,
            "description": variant.description,
            "reference_image": variant.reference_image,
            "variant_id": str(variant.id),
        }
        if (variant := variant_by_asset.get((asset["asset_type"], str(asset["id"]))))
        else asset
        for asset in assets
    ]


async def _asset_records_by_ids_or_names(
    db: AsyncSession,
    model: Any,
    asset_type: str,
    project_id: UUID,
    user_id: UUID,
    asset_ids: Sequence[UUID],
    names: Sequence[Any],
) -> List[Dict[str, Any]]:
    clean_names = [name for name in (_clean_prompt_part(item) for item in names or []) if name]
    if asset_ids:
        identity_condition = model.id.in_(asset_ids)
    elif clean_names:
        identity_condition = model.name.in_(clean_names)
    else:
        return []
    result = await db.execute(
        select(model).where(
            model.project_id == project_id,
            model.user_id == user_id,
            model.is_enabled.is_(True),
            identity_condition,
        )
    )
    return [_asset_payload(asset_type, item) for item in result.scalars().all()]


def _asset_payload(asset_type: str, asset: Any) -> Dict[str, Any]:
    return {
        "asset_type": asset_type,
        "id": str(asset.id),
        "name": asset.name,
        "description": _first_prompt_part(
            getattr(asset, "prompt", None),
            getattr(asset, "description", None),
            getattr(asset, "appearance", None),
            getattr(asset, "environment", None),
        ),
        "reference_image": asset.reference_image,
    }


def _reference_asset_ids(assets: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    result: Dict[str, List[str]] = {"character": [], "scene": [], "prop": []}
    for asset in assets:
        asset_type = str(asset.get("asset_type") or "")
        asset_id = str(asset.get("id") or "")
        if asset_type in result and asset_id and asset_id not in result[asset_type]:
            result[asset_type].append(asset_id)
    return result


async def _resolve_reference_image_urls(db: AsyncSession, urls: List[str]) -> List[str]:
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
    return _dedupe(resolved)


def _material_id_from_image_url(value: Any) -> Optional[UUID]:
    url = _clean_prompt_part(value)
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


def _format_reference_assets(reference_assets: List[Dict[str, Any]]) -> str:
    lines = []
    for asset in reference_assets:
        text = f"- {asset.get('asset_type')}：{asset.get('name')}"
        if asset.get("description"):
            text += f"，描述：{asset['description']}"
        if asset.get("reference_image"):
            text += f"，参考图：{asset['reference_image']}"
        lines.append(text)
    return "\n".join(lines)


def _join_names(value: Any) -> str:
    names = _as_name_list(value)
    return "、".join(names)


def _as_name_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        items = value
    elif isinstance(value, tuple):
        items = list(value)
    else:
        items = [value]
    return [item for item in (_clean_prompt_part(item) for item in items) if item]


def _first_prompt_part(*values: Optional[str]) -> str:
    for value in values:
        cleaned = _clean_prompt_part(value)
        if cleaned:
            return cleaned
    return ""


def _clean_prompt_part(value: Any) -> str:
    return str(value or "").strip()


def _dedupe(values: List[str]) -> List[str]:
    result: List[str] = []
    for value in values:
        item = _clean_prompt_part(value)
        if item and item not in result:
            result.append(item)
    return result


def _first_result_url(content: str) -> str:
    for value in (content or "").split(","):
        url = value.strip()
        if url.startswith(("http://", "https://")):
            return url
    return ""


async def _resolve_image_provider_task(
    model_snapshot: SimpleNamespace, model_result: ModelRunResult
) -> ModelRunResult:
    task_id = model_result.extra.get("task_id")
    if not task_id:
        return model_result

    if settings.provider_task_worker_poll_max_attempts <= 0:
        model_result.extra = {
            **model_result.extra,
            "platform_task_status": "running",
            "provider_polling_deferred": True,
            "next_poll_seconds": provider_poll_interval_seconds("image"),
        }
        model_result.content = f"模型任务仍在生成中：{task_id}"
        return model_result

    latest_result = model_result
    for _ in range(settings.provider_task_worker_poll_max_attempts):
        await asyncio.sleep(settings.provider_task_worker_poll_interval_seconds)
        latest_result = await query_model_task(model_snapshot, "image", str(task_id))
        status = str(latest_result.extra.get("task_status") or "").lower()
        if status in {"failed", "failure", "fail", "error", "canceled", "cancelled"}:
            raise AppException(f"模型任务执行失败：{status}", code=50231, status_code=502)
        if status in {"success", "succeeded", "completed", "complete", "finished", "done"}:
            return latest_result
        if status in {"not_start", "in_progress", "running", "pending", "processing", "queued"}:
            continue
        if latest_result.content and latest_result.content != "生成任务处理中":
            return latest_result

    latest_result.extra = {
        **latest_result.extra,
        "platform_task_status": "running",
        "provider_polling_timeout": True,
        "next_poll_seconds": provider_poll_interval_seconds("image"),
    }
    latest_result.content = f"模型任务仍在生成中：{task_id}"
    return latest_result


async def _mark_storyboard_image_enqueue_failed(
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
        "image_generation_status": "failed",
        "image_generation_failed_reason": "任务入队失败",
    }
    await db.commit()
