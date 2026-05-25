from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin_user
from app.core.responses import success
from app.db.session import get_db
from app.integrations.comfly_video_specs import merge_video_capabilities
from app.models.ai_model import AiModel
from app.models.user import User
from app.schemas.ai_model import (
    AiModelCreateRequest,
    AiModelListOut,
    AiModelOut,
    AiModelUpdateRequest,
    ProviderModelImportOut,
    ProviderModelImportRequest,
    ProviderModelOut,
)
from app.integrations.comfly import list_provider_models
from app.services.ai_models import (
    create_ai_model,
    delete_ai_model,
    get_ai_model_or_404,
    import_provider_models,
    list_ai_models,
    resolve_ai_model_capabilities,
    update_ai_model,
)

router = APIRouter(prefix="/admin/models")


def dump_ai_model(model, schema=AiModelOut) -> dict:
    data = schema.model_validate(model).model_dump(mode="json")
    data["capabilities"] = resolve_ai_model_capabilities(model)
    return data


def normalize_provider_model(item: Any, model_type: str) -> Optional[dict]:
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

    return ProviderModelOut(
        id=model_id,
        model_id=model_id,
        nickname=model_id,
        vendor="comfly",
        model_type=model_type,
        points_cost=0,
        model_multiplier=1,
        cache_multiplier=1,
        completion_multiplier=1,
        platform_multiplier=1,
        is_enabled=True,
        capabilities=merge_video_capabilities(model_id, {}) if model_type == "video" else {},
        object=raw.get("object"),
        owned_by=raw.get("owned_by"),
        root=raw.get("root"),
        parent=raw.get("parent"),
    ).model_dump(mode="json")


async def existing_provider_model_ids(db: AsyncSession, model_type: str) -> set[str]:
    result = await db.execute(
        select(AiModel.model_id).where(
            AiModel.vendor == "comfly",
            AiModel.model_type == model_type,
        )
    )
    return {model_id for model_id in result.scalars().all() if model_id}


@router.get("")
async def admin_list_models(
    keyword: Optional[str] = Query(default=None, max_length=255),
    vendor: Optional[str] = Query(default=None, max_length=64),
    model_type: Optional[str] = Query(default=None, max_length=64),
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
    model_type: str = Query(default="text", max_length=64),
    include_existing: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    existing_model_ids = await existing_provider_model_ids(db, model_type)
    remote_models = await list_provider_models()
    data = []
    for item in remote_models:
        normalized = normalize_provider_model(item, model_type)
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
