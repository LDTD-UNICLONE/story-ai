from typing import List, Optional, Tuple
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException
from app.integrations.comfly_video_specs import merge_video_capabilities
from app.integrations.volcengine_ark_video_specs import (
    VOLCENGINE_ARK_VENDOR,
    is_volcengine_ark_video_model,
    merge_video_capabilities as merge_ark_video_capabilities,
)
from app.models.ai_model import AiModel
from app.schemas.ai_model import (
    AiModelCreateRequest,
    AiModelUpdateRequest,
    ProviderModelImportItem,
)


def resolve_ai_model_capabilities(ai_model: AiModel) -> dict:
    if _is_ark_video_model(ai_model.vendor, ai_model.model_type, ai_model.model_id):
        return merge_ark_video_capabilities(ai_model.capabilities or {})
    if ai_model.vendor == "comfly" and ai_model.model_type == "video":
        return merge_video_capabilities(ai_model.model_id, ai_model.capabilities or {})
    return ai_model.capabilities or {}


def _is_ark_video_model(vendor: str, model_type: str, model_id: str) -> bool:
    return model_type == "video" and (
        vendor == VOLCENGINE_ARK_VENDOR or is_volcengine_ark_video_model(model_id)
    )


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
        query.order_by(AiModel.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
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
    return list(result.scalars().all())


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
    if ai_model is None:
        raise AppException("模型不存在", code=40402, status_code=404)
    return ai_model


async def create_ai_model(db: AsyncSession, payload: AiModelCreateRequest) -> AiModel:
    data = payload.model_dump()
    if _is_ark_video_model(data["vendor"], data["model_type"], data["model_id"]) and not data.get("capabilities"):
        data["capabilities"] = merge_ark_video_capabilities({})
    if data["vendor"] == "comfly" and data["model_type"] == "video" and not data.get("capabilities"):
        data["capabilities"] = merge_video_capabilities(data["model_id"], {})
    ai_model = AiModel(**data)
    db.add(ai_model)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise AppException("模型 ID 已存在", code=40902, status_code=409) from exc

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
        exists = await db.execute(select(AiModel).where(AiModel.model_id == item.model_id))
        if exists.scalar_one_or_none() is not None:
            skipped.append(item.model_id)
            continue

        ai_model = AiModel(
            nickname=item.nickname or item.model_id,
            model_id=item.model_id,
            vendor=item.vendor or default_vendor,
            model_type=item.model_type,
            remark=item.remark,
            points_cost=item.points_cost,
            model_multiplier=item.model_multiplier,
            cache_multiplier=item.cache_multiplier,
            completion_multiplier=item.completion_multiplier,
            platform_multiplier=item.platform_multiplier,
            is_enabled=item.is_enabled,
            capabilities=(
                item.capabilities
                or (
                    merge_ark_video_capabilities({})
                    if _is_ark_video_model(item.vendor or default_vendor, item.model_type, item.model_id)
                    else {}
                )
                or (
                    merge_video_capabilities(item.model_id, {})
                    if (item.vendor or default_vendor) == "comfly" and item.model_type == "video"
                    else {}
                )
            ),
        )
        db.add(ai_model)
        created.append(ai_model)

    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise AppException("导入模型失败，存在重复模型 ID", code=40902, status_code=409) from exc

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

    if "model_id" in update_data and update_data["model_id"] != ai_model.model_id:
        exists = await db.execute(
            select(AiModel).where(AiModel.model_id == update_data["model_id"], AiModel.id != ai_model_id)
        )
        if exists.scalar_one_or_none() is not None:
            raise AppException("模型 ID 已存在", code=40902, status_code=409)

    for field, value in update_data.items():
        setattr(ai_model, field, value)

    if _is_ark_video_model(ai_model.vendor, ai_model.model_type, ai_model.model_id) and not ai_model.capabilities:
        ai_model.capabilities = merge_ark_video_capabilities({})
    if ai_model.vendor == "comfly" and ai_model.model_type == "video" and not ai_model.capabilities:
        ai_model.capabilities = merge_video_capabilities(ai_model.model_id, {})

    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise AppException("模型 ID 已存在", code=40902, status_code=409) from exc

    await db.refresh(ai_model)
    return ai_model


async def delete_ai_model(db: AsyncSession, ai_model_id: UUID) -> AiModel:
    ai_model = await get_ai_model_or_404(db, ai_model_id)
    ai_model.is_enabled = False
    await db.commit()
    await db.refresh(ai_model)
    return ai_model
