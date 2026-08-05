from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.v1.endpoints.task_polling import set_task_poll_headers, task_next_poll_seconds
from app.core.responses import success
from app.db.session import get_db
from app.models.project_asset import ProjectCharacter, ProjectProp, ProjectScene
from app.models.user import User
from app.schemas.project_asset import (
    ProjectCharacterCreateRequest,
    ProjectCharacterListOut,
    ProjectCharacterOut,
    ProjectCharacterUpdateRequest,
    ProjectAssetOptionsOut,
    ProjectAssetImageGenerateOut,
    ProjectAssetImageGenerateRequest,
    ProjectPropCreateRequest,
    ProjectPropListOut,
    ProjectPropOut,
    ProjectPropUpdateRequest,
    ProjectSceneCreateRequest,
    ProjectSceneListOut,
    ProjectSceneOut,
    ProjectSceneUpdateRequest,
)
from app.schemas.project_generated_asset import (
    ProjectGeneratedAssetListOut,
    ProjectGeneratedAssetSelectOut,
    ProjectGeneratedAssetSelectRequest,
    ProjectGeneratedAssetOut,
)
from app.services.project_generated_assets import (
    list_project_generated_asset_history,
    select_project_generated_asset_history,
)
from app.services.project_assets import (
    create_project_asset,
    delete_project_asset,
    get_project_asset_or_404,
    list_project_asset_options,
    list_project_assets,
    update_project_asset,
)
from app.services.project_asset_generation import submit_asset_image_generation

router = APIRouter(prefix="/projects/{project_id}")


@router.get("/characters")
async def my_project_characters(
    project_id: UUID,
    keyword: Optional[str] = Query(default=None, max_length=64),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    items, total = await list_project_assets(
        db, ProjectCharacter, project_id, current_user.id, keyword, page, page_size
    )
    data = ProjectCharacterListOut(
        items=[ProjectCharacterOut.model_validate(item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
    )
    return success(data=data.model_dump(mode="json"))


@router.get("/assets/options")
async def my_project_asset_options(
    project_id: UUID,
    asset_type: str = Query(..., pattern="^(character|scene|prop)$"),
    keyword: Optional[str] = Query(default=None, max_length=64),
    limit: int = Query(default=100, ge=1, le=300),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    model_map = {
        "character": ProjectCharacter,
        "scene": ProjectScene,
        "prop": ProjectProp,
    }
    items, total = await list_project_asset_options(
        db, model_map[asset_type], asset_type, project_id, current_user.id, keyword, limit
    )
    data = ProjectAssetOptionsOut(items=items, total=total)
    return success(data=data.model_dump(mode="json", exclude_none=True))


@router.get("/assets/{asset_type}/{asset_id:uuid}/generation-history")
async def my_project_asset_generation_history(
    project_id: UUID,
    asset_id: UUID,
    asset_type: str = Path(..., pattern="^(character|scene|prop)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    items, total = await list_project_generated_asset_history(
        db,
        project_id=project_id,
        user_id=current_user.id,
        target_type=asset_type,
        target_id=asset_id,
        media_type="image",
        page=page,
        page_size=page_size,
    )
    data = ProjectGeneratedAssetListOut(
        items=[ProjectGeneratedAssetOut.model_validate(item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
    )
    return success(data=data.model_dump(mode="json"))


@router.post("/assets/{asset_type}/{asset_id:uuid}/generation-history/{history_id:uuid}/select")
async def select_my_project_asset_generation_history(
    project_id: UUID,
    asset_id: UUID,
    history_id: UUID,
    payload: ProjectGeneratedAssetSelectRequest,
    asset_type: str = Path(..., pattern="^(character|scene|prop)$"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    history, selected_url, last_frame_url = await select_project_generated_asset_history(
        db,
        project_id=project_id,
        user_id=current_user.id,
        history_id=history_id,
        target_type=asset_type,
        target_id=asset_id,
        media_type="image",
        result_url=payload.result_url,
    )
    data = ProjectGeneratedAssetSelectOut(
        history=ProjectGeneratedAssetOut.model_validate(history),
        selected_url=selected_url,
        last_frame_url=last_frame_url,
    )
    return success(data=data.model_dump(mode="json"), message="选择成功")


@router.get("/characters/options")
async def my_project_character_options(
    project_id: UUID,
    keyword: Optional[str] = Query(default=None, max_length=64),
    limit: int = Query(default=100, ge=1, le=300),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    items, total = await list_project_asset_options(
        db, ProjectCharacter, "character", project_id, current_user.id, keyword, limit
    )
    data = ProjectAssetOptionsOut(items=items, total=total)
    return success(data=data.model_dump(mode="json", exclude_none=True))


@router.post("/characters")
async def create_my_project_character(
    project_id: UUID,
    payload: ProjectCharacterCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    item = await create_project_asset(db, ProjectCharacter, project_id, current_user.id, payload)
    return success(
        data=ProjectCharacterOut.model_validate(item).model_dump(mode="json"), message="创建成功"
    )


@router.get("/characters/{character_id:uuid}")
async def my_project_character_detail(
    project_id: UUID,
    character_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    item = await get_project_asset_or_404(
        db, ProjectCharacter, project_id, character_id, current_user.id
    )
    return success(data=ProjectCharacterOut.model_validate(item).model_dump(mode="json"))


@router.patch("/characters/{character_id:uuid}")
async def update_my_project_character(
    project_id: UUID,
    character_id: UUID,
    payload: ProjectCharacterUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    item = await update_project_asset(
        db, ProjectCharacter, project_id, character_id, current_user.id, payload
    )
    return success(
        data=ProjectCharacterOut.model_validate(item).model_dump(mode="json"), message="更新成功"
    )


@router.delete("/characters/{character_id:uuid}")
async def delete_my_project_character(
    project_id: UUID,
    character_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    item = await delete_project_asset(
        db, ProjectCharacter, project_id, character_id, current_user.id
    )
    return success(
        data=ProjectCharacterOut.model_validate(item).model_dump(mode="json"), message="删除成功"
    )


@router.post("/characters/{character_id:uuid}/image-generation")
async def generate_my_project_character_image(
    project_id: UUID,
    character_id: UUID,
    payload: ProjectAssetImageGenerateRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    item, task_record, points_cost = await submit_asset_image_generation(
        db,
        project_id=project_id,
        asset_type="character",
        asset_id=character_id,
        user=current_user,
        payload=payload,
    )
    data = ProjectAssetImageGenerateOut(
        task_record_id=task_record.id,
        asset_type="character",
        asset_id=item.id,
        status=task_record.status,
        points_cost=points_cost,
        next_poll_seconds=task_next_poll_seconds(task_record),
    )
    set_task_poll_headers(response, data.next_poll_seconds)
    return success(data=data.model_dump(mode="json"), message="任务已提交")


@router.get("/scenes")
async def my_project_scenes(
    project_id: UUID,
    keyword: Optional[str] = Query(default=None, max_length=64),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    items, total = await list_project_assets(
        db, ProjectScene, project_id, current_user.id, keyword, page, page_size
    )
    data = ProjectSceneListOut(
        items=[ProjectSceneOut.model_validate(item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
    )
    return success(data=data.model_dump(mode="json"))


@router.get("/scenes/options")
async def my_project_scene_options(
    project_id: UUID,
    keyword: Optional[str] = Query(default=None, max_length=64),
    limit: int = Query(default=100, ge=1, le=300),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    items, total = await list_project_asset_options(
        db, ProjectScene, "scene", project_id, current_user.id, keyword, limit
    )
    data = ProjectAssetOptionsOut(items=items, total=total)
    return success(data=data.model_dump(mode="json", exclude_none=True))


@router.post("/scenes")
async def create_my_project_scene(
    project_id: UUID,
    payload: ProjectSceneCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    item = await create_project_asset(db, ProjectScene, project_id, current_user.id, payload)
    return success(
        data=ProjectSceneOut.model_validate(item).model_dump(mode="json"), message="创建成功"
    )


@router.get("/scenes/{scene_id:uuid}")
async def my_project_scene_detail(
    project_id: UUID,
    scene_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    item = await get_project_asset_or_404(db, ProjectScene, project_id, scene_id, current_user.id)
    return success(data=ProjectSceneOut.model_validate(item).model_dump(mode="json"))


@router.patch("/scenes/{scene_id:uuid}")
async def update_my_project_scene(
    project_id: UUID,
    scene_id: UUID,
    payload: ProjectSceneUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    item = await update_project_asset(
        db, ProjectScene, project_id, scene_id, current_user.id, payload
    )
    return success(
        data=ProjectSceneOut.model_validate(item).model_dump(mode="json"), message="更新成功"
    )


@router.delete("/scenes/{scene_id:uuid}")
async def delete_my_project_scene(
    project_id: UUID,
    scene_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    item = await delete_project_asset(db, ProjectScene, project_id, scene_id, current_user.id)
    return success(
        data=ProjectSceneOut.model_validate(item).model_dump(mode="json"), message="删除成功"
    )


@router.post("/scenes/{scene_id:uuid}/image-generation")
async def generate_my_project_scene_image(
    project_id: UUID,
    scene_id: UUID,
    payload: ProjectAssetImageGenerateRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    item, task_record, points_cost = await submit_asset_image_generation(
        db,
        project_id=project_id,
        asset_type="scene",
        asset_id=scene_id,
        user=current_user,
        payload=payload,
    )
    data = ProjectAssetImageGenerateOut(
        task_record_id=task_record.id,
        asset_type="scene",
        asset_id=item.id,
        status=task_record.status,
        points_cost=points_cost,
        next_poll_seconds=task_next_poll_seconds(task_record),
    )
    set_task_poll_headers(response, data.next_poll_seconds)
    return success(data=data.model_dump(mode="json"), message="任务已提交")


@router.get("/props")
async def my_project_props(
    project_id: UUID,
    keyword: Optional[str] = Query(default=None, max_length=64),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    items, total = await list_project_assets(
        db, ProjectProp, project_id, current_user.id, keyword, page, page_size
    )
    data = ProjectPropListOut(
        items=[ProjectPropOut.model_validate(item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
    )
    return success(data=data.model_dump(mode="json"))


@router.get("/props/options")
async def my_project_prop_options(
    project_id: UUID,
    keyword: Optional[str] = Query(default=None, max_length=64),
    limit: int = Query(default=100, ge=1, le=300),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    items, total = await list_project_asset_options(
        db, ProjectProp, "prop", project_id, current_user.id, keyword, limit
    )
    data = ProjectAssetOptionsOut(items=items, total=total)
    return success(data=data.model_dump(mode="json", exclude_none=True))


@router.post("/props")
async def create_my_project_prop(
    project_id: UUID,
    payload: ProjectPropCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    item = await create_project_asset(db, ProjectProp, project_id, current_user.id, payload)
    return success(
        data=ProjectPropOut.model_validate(item).model_dump(mode="json"), message="创建成功"
    )


@router.get("/props/{prop_id:uuid}")
async def my_project_prop_detail(
    project_id: UUID,
    prop_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    item = await get_project_asset_or_404(db, ProjectProp, project_id, prop_id, current_user.id)
    return success(data=ProjectPropOut.model_validate(item).model_dump(mode="json"))


@router.patch("/props/{prop_id:uuid}")
async def update_my_project_prop(
    project_id: UUID,
    prop_id: UUID,
    payload: ProjectPropUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    item = await update_project_asset(
        db, ProjectProp, project_id, prop_id, current_user.id, payload
    )
    return success(
        data=ProjectPropOut.model_validate(item).model_dump(mode="json"), message="更新成功"
    )


@router.delete("/props/{prop_id:uuid}")
async def delete_my_project_prop(
    project_id: UUID,
    prop_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    item = await delete_project_asset(db, ProjectProp, project_id, prop_id, current_user.id)
    return success(
        data=ProjectPropOut.model_validate(item).model_dump(mode="json"), message="删除成功"
    )


@router.post("/props/{prop_id:uuid}/image-generation")
async def generate_my_project_prop_image(
    project_id: UUID,
    prop_id: UUID,
    payload: ProjectAssetImageGenerateRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    item, task_record, points_cost = await submit_asset_image_generation(
        db,
        project_id=project_id,
        asset_type="prop",
        asset_id=prop_id,
        user=current_user,
        payload=payload,
    )
    data = ProjectAssetImageGenerateOut(
        task_record_id=task_record.id,
        asset_type="prop",
        asset_id=item.id,
        status=task_record.status,
        points_cost=points_cost,
        next_poll_seconds=task_next_poll_seconds(task_record),
    )
    set_task_poll_headers(response, data.next_poll_seconds)
    return success(data=data.model_dump(mode="json"), message="任务已提交")
