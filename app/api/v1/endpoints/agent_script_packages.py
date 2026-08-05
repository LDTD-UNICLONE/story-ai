from uuid import UUID

from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.responses import success
from app.db.session import get_db
from app.models.user import User
from app.schemas.agent_script_package import (
    AgentScriptPackageConfirmOut,
    AgentScriptPackageConfirmRequest,
    AgentScriptPackageOut,
    AgentScriptSupplementOut,
    AgentScriptSupplementRequest,
)
from app.schemas.agent_story_bible import (
    AgentAssetVariantOut,
    AgentAssetVariantUpdateRequest,
)
from app.services.agent_script_packages import (
    confirm_script_package,
    get_script_package,
    update_script_asset_variant,
)
from app.services.agent_script_supplements import (
    append_agent_script_file,
    append_agent_script_text,
)

router = APIRouter()


@router.post("/agent-productions/{production_id}/script-supplements/from-text")
async def supplement_my_agent_script_from_text(
    production_id: UUID,
    payload: AgentScriptSupplementRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await append_agent_script_text(
        db,
        production_id,
        current_user,
        payload.content,
    )
    data = AgentScriptSupplementOut.model_validate(result)
    return success(data=data.model_dump(mode="json"), message="补充剧本已提交分析")


@router.post("/agent-productions/{production_id}/script-supplements/from-file")
async def supplement_my_agent_script_from_file(
    production_id: UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await append_agent_script_file(
        db,
        production_id,
        current_user,
        file,
    )
    data = AgentScriptSupplementOut.model_validate(result)
    return success(data=data.model_dump(mode="json"), message="补充剧本已提交分析")


@router.get("/agent-productions/{production_id}/script-package")
async def my_agent_script_package(
    production_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    package = await get_script_package(db, production_id, current_user.id)
    data = AgentScriptPackageOut.model_validate(package)
    return success(data=data.model_dump(mode="json"))


@router.patch("/agent-productions/{production_id}/asset-variants/{variant_id}")
async def update_my_agent_asset_variant(
    production_id: UUID,
    variant_id: UUID,
    payload: AgentAssetVariantUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    variant = await update_script_asset_variant(
        db,
        production_id,
        variant_id,
        current_user,
        payload,
    )
    data = AgentAssetVariantOut.model_validate(variant)
    return success(data=data.model_dump(mode="json"), message="资产变体已更新")


@router.post("/agent-productions/{production_id}/script-package/confirm")
async def confirm_my_agent_script_package(
    production_id: UUID,
    payload: AgentScriptPackageConfirmRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await confirm_script_package(db, production_id, current_user, payload)
    data = AgentScriptPackageConfirmOut.model_validate(result)
    return success(data=data.model_dump(mode="json"), message="剧本处理结果已确认")
