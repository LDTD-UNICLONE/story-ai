from decimal import Decimal, ROUND_CEILING
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from sqlalchemy import JSON, bindparam, cast, func, literal_column, or_, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException
from app.integrations.apimart import APIMART_VENDOR
from app.integrations.apimart_image_specs import (
    image_model_capabilities,
    is_apimart_image_model as is_apimart_image_model_id,
    merge_image_capabilities,
)
from app.integrations.apimart_video_specs import (
    is_apimart_video_model as is_apimart_video_model_id,
    merge_video_capabilities as merge_apimart_video_capabilities,
    video_model_capabilities as apimart_video_model_capabilities,
)
from app.integrations.comfly_video_specs import merge_video_capabilities
from app.integrations.volcengine_ark_video_specs import (
    VOLCENGINE_ARK_VENDOR,
    is_volcengine_ark_video_model,
    merge_video_capabilities as merge_ark_video_capabilities,
)
from app.models.ai_model import AiModel
from app.models.task_record import UserTaskRecord
from app.schemas.ai_model import (
    AiModelCreateRequest,
    AiModelUpdateRequest,
    ProviderModelImportItem,
)
from app.services.billing.model_points import (
    build_model_billing_snapshot,
    calculate_apimart_actual_points_cost,
    summarize_base_points_recommendation,
)
from app.services.models.configuration import (
    build_model_configuration,
    model_billing_value,
    model_is_available,
    model_request_capabilities,
    normalize_model_configuration,
)


def resolve_ai_model_capabilities(ai_model: AiModel) -> dict:
    saved_capabilities = model_request_capabilities(ai_model)
    if _is_ark_video_model(ai_model.vendor, ai_model.model_type, ai_model.model_id):
        return merge_ark_video_capabilities(ai_model.model_id, saved_capabilities)
    if _is_comfly_model(ai_model.vendor) and ai_model.model_type == "video":
        return merge_video_capabilities(ai_model.model_id, saved_capabilities)
    if _is_apimart_image_model(ai_model.vendor, ai_model.model_type):
        return merge_image_capabilities(ai_model.model_id, saved_capabilities)
    if _is_apimart_video_model(ai_model.vendor, ai_model.model_type):
        return merge_apimart_video_capabilities(ai_model.model_id, saved_capabilities)
    return saved_capabilities


def resolve_ai_model_configuration(ai_model: AiModel) -> dict:
    configuration = build_model_configuration(ai_model)
    configuration["request"]["capabilities"] = resolve_ai_model_capabilities(ai_model)
    return configuration


def _is_ark_video_model(vendor: str, model_type: str, model_id: str) -> bool:
    return model_type == "video" and vendor == VOLCENGINE_ARK_VENDOR


def _is_comfly_model(vendor: str) -> bool:
    return vendor in {"comfly", "模型服务"}


def _is_apimart_image_model(vendor: str, model_type: str) -> bool:
    return vendor == "apimart" and model_type == "image"


def _is_apimart_video_model(vendor: str, model_type: str) -> bool:
    return vendor == "apimart" and model_type == "video"


async def list_ai_models(
    db: AsyncSession,
    keyword: Optional[str],
    vendor: Optional[str],
    model_type: Optional[str],
    is_enabled: Optional[bool],
    page: int,
    page_size: int,
) -> Tuple[List[AiModel], int]:
    conditions = []
    if keyword:
        pattern = f"%{keyword}%"
        conditions.append(
            or_(
                AiModel.nickname.ilike(pattern),
                AiModel.model_id.ilike(pattern),
                AiModel.vendor.ilike(pattern),
                AiModel.model_type.ilike(pattern),
            )
        )
    if vendor:
        conditions.append(AiModel.vendor == vendor)
    if model_type:
        conditions.append(AiModel.model_type == model_type)
    if is_enabled is not None:
        conditions.append(AiModel.is_enabled == is_enabled)

    query = select(AiModel)
    count_query = select(func.count()).select_from(AiModel)
    if conditions:
        query = query.where(*conditions)
        count_query = count_query.where(*conditions)

    total_result = await db.execute(count_query)
    total = total_result.scalar_one()

    result = await db.execute(
        query.order_by(AiModel.created_at.asc(), AiModel.id.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(result.scalars().all()), total


async def list_ai_model_options(
    db: AsyncSession,
    vendor: Optional[str],
    model_type: Optional[str],
) -> List[AiModel]:
    conditions = []
    conditions.append(AiModel.is_enabled.is_(True))
    if vendor:
        conditions.append(AiModel.vendor == vendor)
    if model_type:
        conditions.append(AiModel.model_type == model_type)

    query = select(AiModel)
    if conditions:
        query = query.where(*conditions)

    result = await db.execute(query.order_by(AiModel.vendor.asc(), AiModel.nickname.asc()))
    return [model for model in result.scalars().all() if model_is_available(model)]


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


async def get_ai_model_or_404(
    db: AsyncSession,
    ai_model_id: UUID,
    only_enabled: bool = False,
) -> AiModel:
    conditions = [AiModel.id == ai_model_id]
    if only_enabled:
        conditions.append(AiModel.is_enabled.is_(True))

    result = await db.execute(select(AiModel).where(*conditions))
    ai_model = result.scalar_one_or_none()
    if ai_model is not None and only_enabled and not model_is_available(ai_model):
        ai_model = None
    if ai_model is None:
        raise AppException("模型不存在", code=40402, status_code=404)
    return ai_model


async def get_ai_model_billing_recommendation(
    db: AsyncSession,
    ai_model: AiModel,
    *,
    sample_limit: int,
) -> Dict[str, Any]:
    if ai_model.vendor != APIMART_VENDOR:
        raise AppException(
            "仅 APIMart 模型支持厂商实际成本基础积分建议",
            code=40005,
            status_code=400,
        )

    result = await db.execute(
        select(UserTaskRecord)
        .where(
            UserTaskRecord.ai_model_id == ai_model.id,
            UserTaskRecord.status == "success",
        )
        .order_by(UserTaskRecord.updated_at.desc(), UserTaskRecord.id.desc())
        .limit(sample_limit)
    )
    records = list(result.scalars().all())
    samples = [
        points
        for record in records
        if (points := _task_recommendation_points(ai_model, record.extra or {})) is not None
    ]
    summary = summarize_base_points_recommendation(
        samples,
        current_base_points=model_billing_value(ai_model, "base_points"),
    )
    recommended = summary["recommended_base_points"]
    return {
        "ai_model_id": ai_model.id,
        "model_id": ai_model.model_id,
        "model_type": ai_model.model_type,
        "vendor": ai_model.vendor,
        "platform_rate": model_billing_value(ai_model, "platform"),
        "recommendation_basis": (
            "provider_cost_points_p95"
            if ai_model.model_type == "image"
            else "platform_charge_points_p95"
        ),
        "evaluated_success_tasks": len(records),
        **summary,
        "suggested_patch": (
            {"configuration": {"billing": {"base_points": recommended}}}
            if recommended is not None and summary["safe_to_apply"]
            else None
        ),
    }


async def create_ai_model(db: AsyncSession, payload: AiModelCreateRequest) -> AiModel:
    data = payload.model_dump()
    data["vendor"] = _normalize_ai_model_vendor(
        data["vendor"], data["model_type"], data["model_id"]
    )
    _validate_agent_default(data["model_type"], data["is_enabled"], data["is_agent_default"])
    data["configuration"] = _normalize_configuration(
        data["vendor"],
        data["model_type"],
        data["model_id"],
        data.get("configuration"),
    )
    if data["is_agent_default"]:
        await _clear_agent_default(db, data["model_type"])
    ai_model = AiModel(**data)
    db.add(ai_model)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise AppException("同一厂商模型 ID 已存在", code=40902, status_code=409) from exc

    await db.refresh(ai_model)
    return ai_model


async def import_provider_models(
    db: AsyncSession,
    models: List[ProviderModelImportItem],
    default_vendor: str,
) -> Tuple[List[AiModel], List[str]]:
    created: List[AiModel] = []
    skipped: List[str] = []

    for item in models:
        vendor = _normalize_ai_model_vendor(
            item.vendor or default_vendor, item.model_type, item.model_id
        )
        exists = await db.execute(
            select(AiModel).where(
                AiModel.vendor == vendor,
                AiModel.model_id == item.model_id,
            )
        )
        if exists.scalar_one_or_none() is not None:
            skipped.append(item.model_id)
            continue

        data = {
            "nickname": item.nickname or item.model_id,
            "model_id": item.model_id,
            "vendor": vendor,
            "model_type": item.model_type,
            "remark": item.remark,
            "is_enabled": item.is_enabled,
            "is_agent_default": item.is_agent_default,
            "configuration": _normalize_configuration(
                vendor,
                item.model_type,
                item.model_id,
                item.configuration,
            ),
        }
        ai_model = AiModel(**data)
        _validate_agent_default(
            ai_model.model_type,
            ai_model.is_enabled,
            ai_model.is_agent_default,
        )
        if ai_model.is_agent_default:
            await _clear_agent_default(db, ai_model.model_type)
        db.add(ai_model)
        created.append(ai_model)

    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise AppException(
            "导入模型失败，同一厂商存在重复模型 ID",
            code=40902,
            status_code=409,
        ) from exc

    for ai_model in created:
        await db.refresh(ai_model)

    return created, skipped


async def update_ai_model(
    db: AsyncSession,
    ai_model_id: UUID,
    payload: AiModelUpdateRequest,
) -> AiModel:
    ai_model = await get_ai_model_or_404(db, ai_model_id)
    update_data = payload.model_dump(exclude_unset=True)

    vendor = update_data.get("vendor", ai_model.vendor)
    model_type = update_data.get("model_type", ai_model.model_type)
    model_id = update_data.get("model_id", ai_model.model_id)
    normalized_vendor = _normalize_ai_model_vendor(vendor, model_type, model_id)
    if normalized_vendor != ai_model.vendor or model_id != ai_model.model_id:
        exists = await db.execute(
            select(AiModel).where(
                AiModel.vendor == normalized_vendor,
                AiModel.model_id == model_id,
                AiModel.id != ai_model_id,
            )
        )
        if exists.scalar_one_or_none() is not None:
            raise AppException("同一厂商模型 ID 已存在", code=40902, status_code=409)

    update_data["vendor"] = normalized_vendor
    changed_routing_fields = _changed_routing_identity_fields(ai_model, update_data)
    if changed_routing_fields & {"model_id", "model_type"}:
        task_count = (
            await db.execute(select(func.count()).where(UserTaskRecord.ai_model_id == ai_model.id))
        ).scalar_one()
        if task_count:
            raise AppException(
                "模型已产生任务，模型 ID 和模型类型不可修改，请新建模型并停用旧模型",
                code=40904,
                status_code=409,
                data={
                    "changed_fields": sorted(changed_routing_fields),
                    "task_count": task_count,
                },
            )
    if "vendor" in changed_routing_fields:
        active_task_count = (
            await db.execute(
                select(func.count()).where(
                    UserTaskRecord.ai_model_id == ai_model.id,
                    UserTaskRecord.status.in_(("pending", "running")),
                )
            )
        ).scalar_one()
        if active_task_count:
            raise AppException(
                "模型存在进行中的任务，暂时不能修改厂商，请等待任务结束后重试",
                code=40904,
                status_code=409,
                data={
                    "changed_fields": ["vendor"],
                    "active_task_count": active_task_count,
                },
            )
        await _backfill_model_billing_snapshots(db, ai_model)

    is_enabled = update_data.get("is_enabled", ai_model.is_enabled)
    is_agent_default = update_data.get("is_agent_default", ai_model.is_agent_default)
    update_data["configuration"] = _normalize_configuration(
        update_data["vendor"],
        model_type,
        model_id,
        update_data.get("configuration"),
        base_configuration=build_model_configuration(ai_model),
    )
    if changed_routing_fields:
        update_data["configuration"]["request"]["capabilities"] = _default_model_capabilities(
            update_data["vendor"], model_type, model_id
        )
    if not is_enabled:
        is_agent_default = False
        update_data["is_agent_default"] = False
    _validate_agent_default(model_type, is_enabled, is_agent_default)
    if is_agent_default:
        await _clear_agent_default(db, model_type, exclude_id=ai_model.id)
    for field, value in update_data.items():
        setattr(ai_model, field, value)

    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise AppException("同一厂商模型 ID 已存在", code=40902, status_code=409) from exc

    await db.refresh(ai_model)
    return ai_model


def _changed_routing_identity_fields(
    ai_model: AiModel,
    update_data: Dict[str, Any],
) -> set[str]:
    return {
        field
        for field in ("vendor", "model_id", "model_type")
        if field in update_data and update_data[field] != getattr(ai_model, field)
    }


async def _backfill_model_billing_snapshots(
    db: AsyncSession,
    ai_model: AiModel,
) -> None:
    snapshot = build_model_billing_snapshot(ai_model)
    extra_jsonb = cast(UserTaskRecord.extra, JSONB)
    await db.execute(
        update(UserTaskRecord)
        .where(
            UserTaskRecord.ai_model_id == ai_model.id,
            extra_jsonb["model_billing_snapshot"].is_(None),
        )
        .values(
            extra=cast(
                func.jsonb_set(
                    extra_jsonb,
                    literal_column("'{model_billing_snapshot}'::text[]"),
                    bindparam(
                        "model_billing_snapshot",
                        value=snapshot,
                        type_=JSONB,
                    ),
                    True,
                ),
                JSON,
            )
        )
    )


def _normalize_configuration(
    vendor: str,
    model_type: str,
    model_id: str,
    configuration: Optional[Dict[str, Any]],
    *,
    base_configuration: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    normalized = normalize_model_configuration(
        vendor=vendor,
        model_type=model_type,
        configuration=configuration,
        base_configuration=base_configuration,
    )
    if not normalized["request"]["capabilities"]:
        normalized["request"]["capabilities"] = _default_model_capabilities(
            vendor, model_type, model_id
        )
    return normalized


def _default_model_capabilities(
    vendor: str,
    model_type: str,
    model_id: str,
) -> Dict[str, Any]:
    if _is_ark_video_model(vendor, model_type, model_id):
        return merge_ark_video_capabilities(model_id, {})
    if _is_comfly_model(vendor) and model_type == "video":
        return merge_video_capabilities(model_id, {})
    if _is_apimart_image_model(vendor, model_type):
        return image_model_capabilities(model_id)
    if _is_apimart_video_model(vendor, model_type):
        return apimart_video_model_capabilities(model_id)
    return {}


async def delete_ai_model(db: AsyncSession, ai_model_id: UUID) -> AiModel:
    ai_model = await get_ai_model_or_404(db, ai_model_id)
    ai_model.is_enabled = False
    ai_model.is_agent_default = False
    await db.commit()
    await db.refresh(ai_model)
    return ai_model


def _task_recommendation_points(
    ai_model: AiModel,
    task_extra: Dict[str, Any],
) -> Optional[int]:
    actual_points, detail = calculate_apimart_actual_points_cost(ai_model, task_extra)
    provider_cost_points = detail.get("provider_cost_points")
    if actual_points is None:
        stored_detail = task_extra.get("provider_cost_billing")
        if not isinstance(stored_detail, dict):
            return None
        provider_cost_points = stored_detail.get("provider_cost_points")

    if isinstance(provider_cost_points, bool):
        return None
    try:
        normalized_provider_points = int(provider_cost_points)
    except (TypeError, ValueError):
        return None
    if normalized_provider_points < 0:
        return None
    if ai_model.model_type == "image":
        return normalized_provider_points
    return max(
        0,
        int(
            (
                Decimal(normalized_provider_points)
                * Decimal(str(model_billing_value(ai_model, "platform")))
            ).quantize(Decimal("1"), rounding=ROUND_CEILING)
        ),
    )


def _normalize_ai_model_vendor(vendor: str, model_type: str, model_id: str) -> str:
    normalized_vendor = vendor.strip()
    if normalized_vendor == "模型服务":
        normalized_vendor = (
            VOLCENGINE_ARK_VENDOR
            if model_type == "video" and is_volcengine_ark_video_model(model_id)
            else "comfly"
        )
    else:
        normalized_vendor = normalized_vendor.lower()

    if normalized_vendor not in {"comfly", VOLCENGINE_ARK_VENDOR, APIMART_VENDOR}:
        raise AppException("暂不支持该模型厂商", code=40005, status_code=400)
    if normalized_vendor == VOLCENGINE_ARK_VENDOR and model_type != "video":
        raise AppException("火山引擎厂商当前仅支持视频模型", code=40005, status_code=400)
    if (
        normalized_vendor == APIMART_VENDOR
        and model_type == "image"
        and not is_apimart_image_model_id(model_id)
    ):
        raise AppException("APIMart 暂不支持该图像模型", code=40005, status_code=400)
    if (
        normalized_vendor == APIMART_VENDOR
        and model_type == "video"
        and not is_apimart_video_model_id(model_id)
    ):
        raise AppException("APIMart 暂不支持该视频模型", code=40005, status_code=400)
    return normalized_vendor


def _validate_agent_default(
    model_type: str,
    is_enabled: bool,
    is_agent_default: bool,
) -> None:
    if not is_agent_default:
        return
    if model_type not in {"text", "image", "video"}:
        raise AppException(
            "Agent 默认模型类型必须是 text、image 或 video",
            code=40061,
            status_code=400,
        )
    if not is_enabled:
        raise AppException(
            "未启用的模型不能设为 Agent 默认模型",
            code=40061,
            status_code=400,
        )


async def _clear_agent_default(
    db: AsyncSession,
    model_type: str,
    exclude_id: Optional[UUID] = None,
) -> None:
    conditions = [
        AiModel.model_type == model_type,
        AiModel.is_agent_default.is_(True),
    ]
    if exclude_id is not None:
        conditions.append(AiModel.id != exclude_id)
    await db.execute(update(AiModel).where(*conditions).values(is_agent_default=False))
