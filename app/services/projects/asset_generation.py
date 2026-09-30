from typing import Any, Dict, Optional, Tuple
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException
from app.core.timezone import beijing_datetime
from app.integrations import apimart, comfly
from app.models.ai_model import AiModel
from app.models.agent_story_bible import AgentAssetVariant
from app.models.project import Project
from app.models.project_asset import ProjectCharacter, ProjectProp, ProjectScene
from app.models.task_record import UserTaskRecord
from app.models.user import User
from app.schemas.project_asset import ProjectAssetImageGenerateRequest
from app.services.generation.task_dispatch import (
    dispatch_tasks_best_effort,
    enqueue_task_dispatch,
)
from app.services.generation.media import extract_result_urls, persist_generated_media_to_oss
from app.services.agent.core_asset_changes import track_core_asset_reference_change
from app.services.billing.model_points import (
    calculate_submission_points_cost,
    settle_image_task_points,
)
from app.services.models.configuration import build_model_runtime_snapshot
from app.services.generation.runner import run_model
from app.services.billing.points import consume_user_points
from app.services.prompts import load_constant_prompt
from app.services.projects.generated_assets import (
    create_project_generated_asset_history,
)
from app.services.projects.assets import get_project_asset_or_404
from app.services.projects.queries import get_owned_enabled_project_with_style_or_404 as get_project_with_style_or_404
from app.services.generation.provider_polling import defer_provider_task_result
from app.services.generation.task_execution import lock_active_task
from app.services.generation.task_records import create_user_task_record
from app.services.generation.provider_state import (
    extract_provider_task_id,
    record_provider_task_state,
)


ASSET_IMAGE_CONFIG: Dict[str, Dict[str, Any]] = {
    "character": {"model": ProjectCharacter, "title": "人物资产图像生成"},
    "scene": {"model": ProjectScene, "title": "场景资产图像生成"},
    "prop": {"model": ProjectProp, "title": "道具资产图像生成"},
}
ASSET_IMAGE_PROMPT_FIELDS = {
    "character": (
        ("名称", "name"),
        ("身份", "identity"),
        ("性别", "gender"),
        ("年龄", "age"),
        ("外貌", "appearance"),
        ("服装", "costume"),
        ("性格气质", "personality"),
        ("描述", "description"),
        ("补充提示", "prompt"),
    ),
    "scene": (
        ("名称", "name"),
        ("地点", "location"),
        ("时间", "time_of_day"),
        ("空间环境", "environment"),
        ("氛围", "atmosphere"),
        ("描述", "description"),
        ("补充提示", "prompt"),
    ),
    "prop": (
        ("名称", "name"),
        ("类别", "category"),
        ("外观", "appearance"),
        ("功能", "function"),
        ("描述", "description"),
        ("补充提示", "prompt"),
    ),
}


async def submit_asset_image_generation(
    db: AsyncSession,
    project_id: UUID,
    asset_type: str,
    asset_id: UUID,
    user: User,
    payload: ProjectAssetImageGenerateRequest,
    *,
    aspect_ratio: Optional[str] = None,
) -> Tuple[Any, UserTaskRecord, int]:
    config = asset_image_config(asset_type)
    project = await get_project_with_style_or_404(db, project_id, user.id)
    asset = await get_project_asset_or_404(db, config["model"], project_id, asset_id, user.id)
    ai_model = await get_enabled_image_model_or_404(db, payload.ai_model_id)
    generation_mode = normalize_generation_mode(payload.generation_mode)

    prompt = build_asset_image_prompt(project, asset, asset_type, generation_mode, payload.prompt)
    generation_ratio = aspect_ratio or project.generation_ratio
    extra = {
        **(payload.extra or {}),
        "aspect_ratio": generation_ratio,
        "generation_mode": generation_mode,
    }
    validate_asset_image_request(ai_model, prompt, extra)
    points_cost = calculate_submission_points_cost(ai_model, "image", extra)

    points_transaction = None
    if points_cost > 0:
        points_transaction = await consume_user_points(
            db,
            user_id=user.id,
            amount=points_cost,
            remark=f"{config['title']}：{asset.name}",
            auto_commit=False,
        )

    task_record = await create_user_task_record(
        db,
        user_id=user.id,
        ai_model_id=ai_model.id,
        points_transaction_id=points_transaction.id if points_transaction else None,
        business_type="project",
        business_id=project_id,
        generation_type="asset_image_generate",
        status="pending",
        title=f"{config['title']}：{asset.name}",
        prompt=prompt,
        result=None,
        points_cost=points_cost,
        extra={
            "project_id": str(project_id),
            "asset_type": asset_type,
            "asset_id": str(asset_id),
            "asset_name": asset.name,
            "generation_mode": generation_mode,
            "generation_ratio": generation_ratio,
            "style_id": str(project.style_id),
            "style_name": project.style.name if project.style else "",
            "model_extra": extra,
        },
    )
    await db.flush()
    asset.extra = {
        **(asset.extra or {}),
        "image_generation_status": "pending",
        "image_generation_task_record_id": str(task_record.id),
    }
    dispatch_id = await enqueue_task_dispatch(
        db,
        task_name="tasks.project_asset_generation.run_project_asset_image_generation",
        args=(str(task_record.id), asset_type, str(asset_id)),
        queue="story_ai_image",
        message_id=task_record.id,
    )
    await db.commit()
    await db.refresh(asset)

    await dispatch_tasks_best_effort(db, [dispatch_id])
    return asset, task_record, points_cost


def validate_asset_image_request(
    ai_model: AiModel, prompt: str, extra: Dict[str, Any]
) -> None:
    if ai_model.vendor == apimart.APIMART_VENDOR:
        apimart.validate_image_request(ai_model.model_id, prompt, extra)
        return
    if ai_model.vendor not in {"comfly", "模型服务"}:
        return
    comfly.validate_image_request(ai_model.model_id, prompt, extra)


async def run_asset_image_generation_in_worker(
    db: AsyncSession,
    task_record: UserTaskRecord,
    asset_type: str,
    asset_id: UUID,
) -> None:
    config = asset_image_config(asset_type)
    asset = await db.get(config["model"], asset_id)
    if asset is None or not asset.is_enabled:
        raise AppException("项目资源不存在", code=40409, status_code=404)
    variant = await get_task_asset_variant(db, task_record)

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

    model_snapshot = build_model_runtime_snapshot(ai_model)
    model_result = await run_model(
        model_snapshot,
        "image",
        task_record.prompt,
        (task_record.extra or {}).get("model_extra") or {},
        idempotency_key=str(task_record.id),
    )
    if extract_provider_task_id(model_result.extra):
        if not await lock_active_task(db, task_record):
            return
        record_provider_task_state(task_record, model_result.extra, ai_model.vendor)
        task_record.extra = {
            **(task_record.extra or {}),
            "model_result_extra": model_result.extra,
        }
        await db.commit()
    provider_task_id = extract_provider_task_id(model_result.extra)
    if provider_task_id:
        model_result = defer_provider_task_result(model_result, provider_task_id)
    else:
        model_result = await persist_generated_media_to_oss("image", model_result)
    if not await lock_active_task(db, task_record):
        return
    await db.refresh(asset)
    if variant is not None:
        await db.refresh(variant)
    record_provider_task_state(task_record, model_result.extra, ai_model.vendor)

    if model_result.extra.get("platform_task_status") == "running":
        set_asset_image_generation_state(
            asset,
            variant,
            status="running",
            task_record_id=task_record.id,
            generation_extra=model_result.extra,
        )
        task_record.status = "running"
        task_record.result = model_result.content
        task_record.extra = {
            **(task_record.extra or {}),
            "model_result_extra": model_result.extra,
        }
        return

    image_url = _first_result_url(model_result.content)
    if not image_url:
        raise AppException("图像生成未返回有效结果", code=50231, status_code=502)

    history = await create_project_generated_asset_history(
        db,
        task_record=task_record,
        target_type="asset_variant" if variant is not None else asset_type,
        target_id=variant.id if variant is not None else asset_id,
        media_type="image",
        result_urls=extract_result_urls(model_result.content) or [image_url],
        result_url=image_url,
        generation_mode=(task_record.extra or {}).get("generation_mode"),
        extra={
            "asset_name": asset.name,
            **({"variant_name": variant.canonical_name} if variant is not None else {}),
            "generation_ratio": (task_record.extra or {}).get("generation_ratio"),
            "model_result_extra": model_result.extra,
        },
    )
    if variant is not None:
        await track_core_asset_reference_change(
            db,
            project_id=asset.project_id,
            user_id=asset.user_id,
            asset_type=asset_type,
            asset_id=asset.id,
            variant_id=variant.id,
            previous_reference_image=variant.reference_image,
            new_reference_image=image_url,
            source="worker",
        )
        variant.reference_image = image_url
        variant.lock_version += 1
        variant.extra = {
            **(variant.extra or {}),
            "image_generation_status": "success",
            "image_generation_history_id": str(history.id),
            "image_generation_task_record_id": str(task_record.id),
            "image_generation_extra": model_result.extra,
        }
        variant.updated_at = beijing_datetime()
    else:
        await track_core_asset_reference_change(
            db,
            project_id=asset.project_id,
            user_id=asset.user_id,
            asset_type=asset_type,
            asset_id=asset.id,
            previous_reference_image=asset.reference_image,
            new_reference_image=image_url,
            source="worker",
        )
        asset.reference_image = image_url
        asset.updated_at = beijing_datetime()
        asset.extra = {
            **(asset.extra or {}),
            "image_generation_status": "success",
            "image_generation_history_id": str(history.id),
            "image_generation_task_record_id": str(task_record.id),
            "image_generation_extra": model_result.extra,
        }
    task_record.status = "success"
    task_record.result = image_url
    task_record.extra = {
        **(task_record.extra or {}),
        "model_result_extra": model_result.extra,
        "oss_image_url": image_url,
        "generated_asset_history_id": str(history.id),
    }
    await settle_image_task_points(
        db,
        task_record,
        ai_model,
        model_result.extra,
        remark_prefix="资产图像生成",
    )


async def get_task_asset_variant(
    db: AsyncSession,
    task_record: UserTaskRecord,
) -> Optional[AgentAssetVariant]:
    raw_variant_id = (task_record.extra or {}).get("agent_asset_variant_id")
    if not raw_variant_id:
        return None
    try:
        variant_id = UUID(str(raw_variant_id))
    except ValueError as exc:
        raise AppException("资产变体任务数据无效", code=50043, status_code=500) from exc
    variant = await db.get(AgentAssetVariant, variant_id)
    if variant is None or variant.review_status == "rejected":
        raise AppException("资产变体不存在", code=40440, status_code=404)
    return variant


def set_asset_image_generation_state(
    asset: Any,
    variant: Optional[AgentAssetVariant],
    *,
    status: str,
    task_record_id: UUID,
    generation_extra: Optional[Dict[str, Any]] = None,
    failed_reason: Optional[str] = None,
) -> None:
    target = variant if variant is not None else asset
    state = variant.extra if variant is not None else asset.extra
    updated = {
        **(state or {}),
        "image_generation_status": status,
        "image_generation_task_record_id": str(task_record_id),
    }
    if generation_extra is not None:
        updated["image_generation_extra"] = generation_extra
    if failed_reason is not None:
        updated["image_generation_failed_reason"] = failed_reason
    if variant is not None:
        variant.extra = updated
    else:
        asset.extra = updated
    target.updated_at = beijing_datetime()


async def get_enabled_image_model_or_404(db: AsyncSession, ai_model_id: UUID) -> AiModel:
    result = await db.execute(
        select(AiModel).where(
            AiModel.id == ai_model_id,
            AiModel.model_type == "image",
            AiModel.is_enabled.is_(True),
        )
    )
    ai_model = result.scalar_one_or_none()
    if ai_model is None:
        raise AppException("图像模型不存在、未启用或类型不匹配", code=40404, status_code=404)
    return ai_model


def build_asset_image_prompt(
    project: Project,
    asset: Any,
    asset_type: str,
    generation_mode: str = "general",
    custom_prompt: Optional[str] = None,
) -> str:
    style_prompt = project.style.prompt if project.style else ""
    constant_prompt = load_asset_image_constant_prompt(asset_type, generation_mode)
    asset_prompt = _build_asset_image_prompt_text(asset, asset_type)
    return "\n".join(
        item
        for item in (
            f"画面风格：{style_prompt}",
            f"模式提示词：{constant_prompt}",
            f"资产提示词：{asset_prompt}",
            f"用户补充要求：{custom_prompt.strip()}"
            if custom_prompt and custom_prompt.strip()
            else "",
            "请严格围绕该资产生成图像，不要添加与资产无关的主体内容。",
        )
        if item
    )


def _build_asset_image_prompt_text(asset: Any, asset_type: str) -> str:
    values = []
    for label, field in ASSET_IMAGE_PROMPT_FIELDS[asset_type]:
        value = _clean_optional_text(getattr(asset, field, None))
        if value:
            values.append(f"{label}：{value}")
    return "；".join(values)


def _clean_optional_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_generation_mode(generation_mode: str) -> str:
    value = (generation_mode or "general").strip()
    if not value:
        return "general"
    aliases = {
        "card": "profile_card",
        "data_card": "profile_card",
        "profile-card": "profile_card",
        "资料卡": "profile_card",
        "资料卡模式": "profile_card",
    }
    value = aliases.get(value, value)
    if not all(char.isalnum() or char in {"_", "-"} for char in value):
        raise AppException("生成模式格式不正确", code=40014, status_code=400)
    return value


def load_asset_image_constant_prompt(asset_type: str, generation_mode: str) -> str:
    asset_image_config(asset_type)
    try:
        return load_constant_prompt(f"asset_image_generation/{asset_type}/{generation_mode}.md")
    except AppException as exc:
        if exc.code == 50041:
            raise AppException("资产图像生成模式不存在", code=40015, status_code=400) from exc
        raise


def asset_image_config(asset_type: str) -> Dict[str, Any]:
    config = ASSET_IMAGE_CONFIG.get(asset_type)
    if not config:
        raise AppException("不支持的资产图像生成类型", code=40013, status_code=400)
    return config


def _first_result_url(content: str) -> str:
    if not content:
        return ""
    return content.split(",", 1)[0].strip()

