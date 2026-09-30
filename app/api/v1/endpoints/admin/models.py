from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin_user
from app.core.exceptions import AppException
from app.core.responses import success
from app.db.session import get_db
from app.integrations.apimart import APIMART_VENDOR
from app.integrations.apimart import list_provider_models as list_apimart_models
from app.integrations.apimart_image_specs import (
    image_model_capabilities,
    is_apimart_image_model,
)
from app.integrations.apimart_video_specs import (
    is_apimart_video_model,
    video_model_capabilities,
)
from app.integrations.comfly import list_provider_models as list_comfly_models
from app.integrations.comfly_video_specs import merge_video_capabilities
from app.models.ai_model import AiModel
from app.models.user import User
from app.schemas.ai_model import (
    AiModelBillingRecommendationOut,
    AiModelCreateRequest,
    AiModelListOut,
    AiModelOut,
    AiModelUpdateRequest,
    ProviderModelImportOut,
    ProviderModelImportRequest,
    ProviderModelOut,
)
from app.services.models.catalog import (
    create_ai_model,
    delete_ai_model,
    get_ai_model_or_404,
    get_ai_model_billing_recommendation,
    import_provider_models,
    list_ai_models,
    resolve_ai_model_configuration,
    update_ai_model,
)
from app.services.models.configuration import normalize_model_configuration

router = APIRouter(prefix="/admin/models")


def dump_ai_model(model, schema=AiModelOut) -> dict:
    data = schema.model_validate(model).model_dump(mode="json")
    data["configuration"] = resolve_ai_model_configuration(model)
    return data


def normalize_provider_model(item: Any, model_type: str, vendor: str) -> Optional[dict]:
    if isinstance(item, str):
        model_id = item
        raw = {}
    elif isinstance(item, dict):
        model_id = str(item.get("id") or item.get("model") or item.get("model_id") or "")
        raw = item
    else:
        return None

    if not model_id:
        return None
    if vendor == APIMART_VENDOR:
        is_image = is_apimart_image_model(model_id)
        is_video = is_apimart_video_model(model_id)
        if model_type == "image" and not is_image:
            return None
        if model_type == "video" and not is_video:
            return None
        if model_type == "text" and (is_image or is_video):
            return None

    capabilities = (
        merge_video_capabilities(model_id, {})
        if vendor == "comfly" and model_type == "video"
        else image_model_capabilities(model_id)
        if vendor == APIMART_VENDOR and model_type == "image"
        else video_model_capabilities(model_id)
        if vendor == APIMART_VENDOR and model_type == "video"
        else {}
    )
    return ProviderModelOut(
        id=model_id,
        model_id=model_id,
        nickname=model_id,
        vendor=vendor,
        model_type=model_type,
        is_enabled=True,
        configuration=normalize_model_configuration(
            vendor=vendor,
            model_type=model_type,
            configuration={"request": {"capabilities": capabilities}},
        ),
        object=raw.get("object"),
        owned_by=raw.get("owned_by"),
        root=raw.get("root"),
        parent=raw.get("parent"),
    ).model_dump(mode="json")


async def existing_provider_model_ids(db: AsyncSession, vendor: str) -> set[str]:
    result = await db.execute(select(AiModel.model_id).where(AiModel.vendor == vendor))
    return {model_id for model_id in result.scalars().all() if model_id}


@router.get("")
async def admin_list_models(
    keyword: Optional[str] = Query(default=None, max_length=255),
    vendor: Optional[str] = Query(default=None, max_length=64),
    model_type: Optional[str] = Query(default=None, pattern="^(text|image|video)$"),
    is_enabled: Optional[bool] = Query(default=True),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    models, total = await list_ai_models(
        db,
        keyword=keyword,
        vendor=vendor,
        model_type=model_type,
        is_enabled=is_enabled,
        page=page,
        page_size=page_size,
    )
    data = AiModelListOut(
        items=[AiModelOut(**dump_ai_model(item)) for item in models],
        total=total,
        page=page,
        page_size=page_size,
    )
    return success(data=data.model_dump(mode="json"))


@router.post("")
async def admin_create_model(
    payload: AiModelCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    ai_model = await create_ai_model(db, payload)
    return success(data=dump_ai_model(ai_model), message="创建成功")


@router.get("/provider/available")
async def admin_list_provider_models(
    vendor: str = Query(default="comfly", max_length=64),
    model_type: str = Query(default="text", pattern="^(text|image|video)$"),
    include_existing: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    normalized_vendor = vendor.strip().lower()
    if normalized_vendor == "comfly":
        remote_models = await list_comfly_models()
    elif normalized_vendor == APIMART_VENDOR:
        remote_models = await list_apimart_models()
    else:
        raise AppException("该厂商不支持远程获取模型列表", code=40005, status_code=400)
    existing_model_ids = await existing_provider_model_ids(db, normalized_vendor)
    data = []
    for item in remote_models:
        normalized = normalize_provider_model(item, model_type, normalized_vendor)
        if normalized and not include_existing and normalized["model_id"] in existing_model_ids:
            continue
        if normalized:
            data.append(normalized)
    return success(data=data)


@router.post("/provider/import")
async def admin_import_provider_models(
    payload: ProviderModelImportRequest,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    created, skipped = await import_provider_models(
        db,
        models=payload.models,
        default_vendor="comfly",
    )
    data = ProviderModelImportOut(
        created=[AiModelOut(**dump_ai_model(item)) for item in created],
        skipped=skipped,
    )
    return success(data=data.model_dump(mode="json"), message="导入完成")


@router.get("/{ai_model_id}/billing-recommendation")
async def admin_get_model_billing_recommendation(
    ai_model_id: UUID,
    sample_limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    ai_model = await get_ai_model_or_404(db, ai_model_id)
    recommendation = await get_ai_model_billing_recommendation(
        db,
        ai_model,
        sample_limit=sample_limit,
    )
    data = AiModelBillingRecommendationOut(**recommendation)
    return success(data=data.model_dump(mode="json"))


@router.get("/{ai_model_id}")
async def admin_get_model(
    ai_model_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    ai_model = await get_ai_model_or_404(db, ai_model_id)
    return success(data=dump_ai_model(ai_model))


@router.patch("/{ai_model_id}")
async def admin_update_model(
    ai_model_id: UUID,
    payload: AiModelUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    ai_model = await update_ai_model(db, ai_model_id, payload)
    return success(data=dump_ai_model(ai_model), message="更新成功")


@router.delete("/{ai_model_id}")
async def admin_delete_model(
    ai_model_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    ai_model = await delete_ai_model(db, ai_model_id)
    return success(data=dump_ai_model(ai_model), message="删除成功")
