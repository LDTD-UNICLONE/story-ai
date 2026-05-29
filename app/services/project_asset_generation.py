import asyncio
from types import SimpleNamespace
from typing import Any, Dict, Optional, Tuple
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.exceptions import AppException
from app.core.timezone import beijing_datetime
from app.models.ai_model import AiModel
from app.models.project import Project
from app.models.project_asset import ProjectCharacter, ProjectProp, ProjectScene
from app.models.task_record import UserTaskRecord
from app.models.user import User
from app.schemas.project_asset import ProjectAssetImageGenerateRequest
from app.services.generated_media import persist_generated_media_to_oss
from app.core.config import settings
from app.services.model_points import calculate_submission_points_cost
from app.services.model_runner import ModelRunResult, query_model_task, run_model
from app.services.points import change_user_points, consume_user_points
from app.services.prompts import load_constant_prompt
from app.services.project_assets import get_project_asset_or_404
from app.services.projects import get_project_or_404
from app.services.task_records import create_user_task_record, refresh_task_record_interrupted


ASSET_IMAGE_CONFIG: Dict[str, Dict[str, Any]] = {
    "character": {"model": ProjectCharacter, "title": "人物资产图像生成"},
    "scene": {"model": ProjectScene, "title": "场景资产图像生成"},
    "prop": {"model": ProjectProp, "title": "道具资产图像生成"},
}


async def submit_asset_image_generation(
    db: AsyncSession,
    project_id: UUID,
    asset_type: str,
    asset_id: UUID,
    user: User,
    payload: ProjectAssetImageGenerateRequest,
) -> Tuple[Any, UserTaskRecord, int]:
    config = _asset_image_config(asset_type)
    project = await get_project_with_style_or_404(db, project_id, user.id)
    asset = await get_project_asset_or_404(db, config["model"], project_id, asset_id, user.id)
    ai_model = await get_enabled_image_model_or_404(db, payload.ai_model_id)
    generation_mode = normalize_generation_mode(payload.generation_mode)
    points_cost = calculate_submission_points_cost(ai_model, "image", payload.extra or {})

    points_transaction = None
    if points_cost > 0:
        points_transaction = await consume_user_points(
            db,
            user_id=user.id,
            amount=points_cost,
            remark=f"{config['title']}：{asset.name}",
            auto_commit=False,
        )

    prompt = build_asset_image_prompt(project, asset, asset_type, generation_mode, payload.prompt)
    extra = {
        **(payload.extra or {}),
        "aspect_ratio": project.generation_ratio,
        "generation_mode": generation_mode,
    }
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
            "generation_ratio": project.generation_ratio,
            "style_id": str(project.style_id),
            "style_name": project.style.name if project.style else "",
            "model_extra": extra,
        },
    )
    asset.extra = {
        **(asset.extra or {}),
        "image_generation_status": "pending",
        "image_generation_task_record_id": str(task_record.id),
    }
    await db.commit()
    await db.refresh(asset)

    try:
        from app.tasks.project_asset_generation import run_project_asset_image_generation

        run_project_asset_image_generation.delay(str(task_record.id), asset_type, str(asset_id))
    except Exception:
        await _mark_asset_image_enqueue_failed(db, task_record, asset)
        await db.refresh(asset)
    return asset, task_record, points_cost


async def run_asset_image_generation_in_worker(
    db: AsyncSession,
    task_record: UserTaskRecord,
    asset_type: str,
    asset_id: UUID,
) -> None:
    config = _asset_image_config(asset_type)
    asset = await db.get(config["model"], asset_id)
    if asset is None or not asset.is_enabled:
        raise AppException("项目资源不存在", code=40409, status_code=404)

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
    )
    model_result = await _resolve_image_provider_task(model_snapshot, model_result)
    model_result = await persist_generated_media_to_oss("image", model_result)
    if await refresh_task_record_interrupted(db, task_record):
        return

    if model_result.extra.get("platform_task_status") == "running":
        asset.extra = {
            **(asset.extra or {}),
            "image_generation_status": "running",
            "image_generation_task_record_id": str(task_record.id),
            "image_generation_extra": model_result.extra,
        }
        asset.updated_at = beijing_datetime()
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

    asset.reference_image = image_url
    asset.updated_at = beijing_datetime()
    asset.extra = {
        **(asset.extra or {}),
        "image_generation_status": "success",
        "image_generation_task_record_id": str(task_record.id),
        "image_generation_extra": model_result.extra,
    }
    task_record.status = "success"
    task_record.result = image_url
    task_record.extra = {
        **(task_record.extra or {}),
        "model_result_extra": model_result.extra,
        "oss_image_url": image_url,
    }


async def get_project_with_style_or_404(db: AsyncSession, project_id: UUID, user_id: UUID) -> Project:
    await get_project_or_404(db, project_id, user_id)
    result = await db.execute(
        select(Project)
        .options(selectinload(Project.style))
        .where(
            Project.id == project_id,
            Project.user_id == user_id,
            Project.is_enabled.is_(True),
        )
    )
    project = result.scalar_one_or_none()
    if project is None:
        raise AppException("项目不存在", code=40407, status_code=404)
    if project.style is None or not project.style.is_enabled:
        raise AppException("项目风格不存在或未启用", code=40403, status_code=404)
    return project


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
    asset_prompt = custom_prompt or asset.prompt or asset.description or asset.name
    style_prompt = project.style.prompt if project.style else ""
    constant_prompt = load_asset_image_constant_prompt(asset_type, generation_mode)
    return "\n".join(
        item
        for item in (
            f"整体风格：{style_prompt}",
            f"生成模式：{generation_mode}",
            f"模式提示词：{constant_prompt}",
            f"生成比例：{project.generation_ratio}",
            f"资产名称：{asset.name}",
            f"资产描述：{asset_prompt}",
            "请严格围绕该资产生成图像，不要添加与资产无关的主体内容。",
        )
        if item
    )


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
    _asset_image_config(asset_type)
    try:
        return load_constant_prompt(f"asset_image_generation/{asset_type}/{generation_mode}.md")
    except AppException as exc:
        if exc.code == 50041:
            raise AppException("资产图像生成模式不存在", code=40015, status_code=400) from exc
        raise


def _asset_image_config(asset_type: str) -> Dict[str, Any]:
    config = ASSET_IMAGE_CONFIG.get(asset_type)
    if not config:
        raise AppException("不支持的资产图像生成类型", code=40013, status_code=400)
    return config


def _first_result_url(content: str) -> str:
    if not content:
        return ""
    return content.split(",", 1)[0].strip()


async def _resolve_image_provider_task(model_snapshot: SimpleNamespace, model_result: ModelRunResult) -> ModelRunResult:
    task_id = model_result.extra.get("task_id")
    if not task_id:
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
        "next_poll_seconds": settings.provider_task_poll_interval_seconds,
    }
    latest_result.content = f"模型任务仍在生成中：{task_id}"
    return latest_result


async def _mark_asset_image_enqueue_failed(db: AsyncSession, task_record: UserTaskRecord, asset: Any) -> None:
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
    asset.extra = {
        **(asset.extra or {}),
        "image_generation_status": "failed",
        "image_generation_failed_reason": "任务入队失败",
    }
    await db.commit()
