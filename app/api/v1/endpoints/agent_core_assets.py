from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.responses import success
from app.db.session import get_db
from app.models.user import User
from app.schemas.agent_core_asset import (
    CoreAssetConfirmRequest,
    CoreAssetCreateRequest,
    CoreAssetDeleteOut,
    CoreAssetImageGenerationOut,
    CoreAssetImageGenerationRequest,
    CoreAssetImpactOut,
    CoreAssetLockOut,
    CoreAssetLockRequest,
    CoreAssetManageItem,
    CoreAssetManageListOut,
    CoreAssetReadinessOut,
    CoreAssetReferenceImageRequest,
    CoreAssetSelectionRequest,
    CoreAssetUpdateRequest,
    CoreAssetVariantCreateRequest,
    CoreAssetVariantDeleteOut,
    CoreAssetVariantImageGenerationOut,
    CoreAssetVariantImageGenerationRequest,
    CoreAssetVariantOut,
)
from app.services.agent.core_assets import (
    confirm_core_assets,
    create_core_asset,
    create_core_asset_variant,
    delete_core_asset,
    delete_core_asset_variant,
    get_core_asset,
    get_core_asset_readiness,
    list_core_assets,
    lock_core_assets,
    preview_core_asset_impact,
    review_core_asset_reference_image,
    submit_core_asset_image_generations,
    submit_core_asset_variant_image_generation,
    update_core_asset,
    update_core_asset_reference_image,
    update_core_asset_variant_reference_image,
)
from app.services.agent.workflow_steps import require_agent_step_access


async def require_core_asset_step(
    production_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await require_agent_step_access(db, production_id, current_user.id, 2)


router = APIRouter(dependencies=[Depends(require_core_asset_step)])


@router.post(
    "/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}/reference-image/review"
)
async def review_my_core_asset_reference_image(
    production_id: UUID,
    asset_type: Literal["character", "scene", "prop"],
    asset_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await review_core_asset_reference_image(
        db, production_id, current_user.id, asset_type, asset_id,
    )
    return success(data=result.model_dump(mode="json"), message="图片审核状态已获取")


@router.post(
    "/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}/variants/"
    "{variant_id}/reference-image/review"
)
async def review_my_core_asset_variant_reference_image(
    production_id: UUID,
    asset_type: Literal["character", "scene", "prop"],
    asset_id: UUID,
    variant_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await review_core_asset_reference_image(
        db, production_id, current_user.id, asset_type, asset_id, variant_id=variant_id,
    )
    return success(data=result.model_dump(mode="json"), message="图片审核状态已获取")


@router.get("/agent-productions/{production_id}/core-assets")
async def my_core_assets(
    production_id: UUID,
    asset_type: Optional[Literal["character", "scene", "prop"]] = Query(default=None),
    keyword: Optional[str] = Query(default=None, min_length=1, max_length=128),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await list_core_assets(
        db,
        production_id,
        current_user.id,
        asset_type=asset_type,
        keyword=keyword,
        page=page,
        page_size=page_size,
    )
    data = CoreAssetManageListOut.model_validate(result)
    return success(data=data.model_dump(mode="json"))


@router.post("/agent-productions/{production_id}/core-assets")
async def create_my_core_asset(
    production_id: UUID,
    payload: CoreAssetCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await create_core_asset(db, production_id, current_user.id, payload)
    data = CoreAssetManageItem.model_validate(result)
    return success(data=data.model_dump(mode="json"), message="核心资产已创建")


@router.get("/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}")
async def my_core_asset(
    production_id: UUID,
    asset_type: Literal["character", "scene", "prop"],
    asset_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await get_core_asset(db, production_id, current_user.id, asset_type, asset_id)
    data = CoreAssetManageItem.model_validate(result)
    return success(data=data.model_dump(mode="json"))


@router.patch("/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}")
async def update_my_core_asset(
    production_id: UUID,
    asset_type: Literal["character", "scene", "prop"],
    asset_id: UUID,
    payload: CoreAssetUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await update_core_asset(
        db,
        production_id,
        current_user.id,
        asset_type,
        asset_id,
        payload,
    )
    data = CoreAssetManageItem.model_validate(result)
    return success(data=data.model_dump(mode="json"), message="核心资产已更新")


@router.delete("/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}")
async def delete_my_core_asset(
    production_id: UUID,
    asset_type: Literal["character", "scene", "prop"],
    asset_id: UUID,
    expected_lock_version: int = Query(..., ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await delete_core_asset(
        db,
        production_id,
        current_user.id,
        asset_type,
        asset_id,
        expected_lock_version,
    )
    data = CoreAssetDeleteOut.model_validate(result)
    return success(data=data.model_dump(mode="json"), message="核心资产已删除")


@router.post(
    "/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}/variants"
)
async def create_my_core_asset_variant(
    production_id: UUID,
    asset_type: Literal["character", "scene", "prop"],
    asset_id: UUID,
    payload: CoreAssetVariantCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    variant = await create_core_asset_variant(
        db,
        production_id,
        current_user.id,
        asset_type,
        asset_id,
        payload,
    )
    data = CoreAssetVariantOut.model_validate(variant)
    return success(data=data.model_dump(mode="json"), message="资产变体已创建")


@router.delete(
    "/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}/variants/{variant_id}"
)
async def delete_my_core_asset_variant(
    production_id: UUID,
    asset_type: Literal["character", "scene", "prop"],
    asset_id: UUID,
    variant_id: UUID,
    expected_lock_version: int = Query(..., ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await delete_core_asset_variant(
        db,
        production_id,
        current_user.id,
        asset_type,
        asset_id,
        variant_id,
        expected_lock_version,
    )
    data = CoreAssetVariantDeleteOut.model_validate(result)
    return success(data=data.model_dump(mode="json"), message="资产变体已删除")


@router.put(
    "/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}/variants/"
    "{variant_id}/reference-image"
)
async def update_my_core_asset_variant_reference_image(
    production_id: UUID,
    asset_type: Literal["character", "scene", "prop"],
    asset_id: UUID,
    variant_id: UUID,
    payload: CoreAssetReferenceImageRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    variant = await update_core_asset_variant_reference_image(
        db,
        production_id,
        current_user.id,
        asset_type,
        asset_id,
        variant_id,
        payload,
    )
    data = CoreAssetVariantOut.model_validate(variant)
    return success(data=data.model_dump(mode="json"), message="资产变体参考图已自动采用")


@router.post(
    "/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}/variants/"
    "{variant_id}/image-generation"
)
async def generate_my_core_asset_variant_image(
    production_id: UUID,
    asset_type: Literal["character", "scene", "prop"],
    asset_id: UUID,
    variant_id: UUID,
    payload: CoreAssetVariantImageGenerationRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await submit_core_asset_variant_image_generation(
        db,
        production_id,
        current_user,
        asset_type,
        asset_id,
        variant_id,
        payload,
    )
    data = CoreAssetVariantImageGenerationOut.model_validate(result)
    return success(data=data.model_dump(mode="json"), message="资产变体参考图任务已提交")


@router.get("/agent-productions/{production_id}/core-assets/readiness")
async def my_core_asset_readiness(
    production_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await get_core_asset_readiness(db, production_id, current_user.id)
    data = CoreAssetReadinessOut.model_validate(result)
    return success(data=data.model_dump(mode="json"))


@router.post("/agent-productions/{production_id}/core-assets/image-generations")
async def generate_my_core_asset_images(
    production_id: UUID,
    payload: CoreAssetImageGenerationRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await submit_core_asset_image_generations(
        db,
        production_id,
        current_user,
        payload,
    )
    data = CoreAssetImageGenerationOut.model_validate(result)
    return success(data=data.model_dump(mode="json"), message="核心资产参考图任务已提交")


@router.put(
    "/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}/reference-image"
)
async def update_my_core_asset_reference_image(
    production_id: UUID,
    asset_type: Literal["character", "scene", "prop"],
    asset_id: UUID,
    payload: CoreAssetReferenceImageRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await update_core_asset_reference_image(
        db,
        production_id,
        current_user.id,
        asset_type,
        asset_id,
        payload,
    )
    data = CoreAssetManageItem.model_validate(result)
    return success(data=data.model_dump(mode="json"), message="资产参考图已自动采用")


@router.post("/agent-productions/{production_id}/core-assets/confirm")
async def confirm_my_core_assets(
    production_id: UUID,
    payload: CoreAssetConfirmRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await confirm_core_assets(db, production_id, current_user, payload)
    data = CoreAssetLockOut.model_validate(result)
    return success(data=data.model_dump(mode="json"), message="资产图像已确认")


@router.post("/agent-productions/{production_id}/core-assets/impact-preview")
async def my_core_asset_impact_preview(
    production_id: UUID,
    payload: CoreAssetSelectionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await preview_core_asset_impact(
        db,
        production_id,
        current_user.id,
        payload,
    )
    data = CoreAssetImpactOut.model_validate(result)
    return success(data=data.model_dump(mode="json"))


@router.post("/agent-productions/{production_id}/core-assets/lock")
async def lock_my_core_assets(
    production_id: UUID,
    payload: CoreAssetLockRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await lock_core_assets(db, production_id, current_user, payload)
    data = CoreAssetLockOut.model_validate(result)
    return success(data=data.model_dump(mode="json"), message="核心资产已锁定")
