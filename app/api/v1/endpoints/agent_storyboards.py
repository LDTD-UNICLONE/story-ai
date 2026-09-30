from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.responses import success
from app.db.session import get_db
from app.models.user import User
from app.schemas.agent_storyboard import (
    AgentStoryboardCopyRequest,
    AgentStoryboardCreateRequest,
    AgentStoryboardDeleteOut,
    AgentStoryboardGenerateRequest,
    AgentStoryboardItemOut,
    AgentStoryboardOrderOut,
    AgentStoryboardPackageOut,
    AgentStoryboardReorderRequest,
    AgentStoryboardUpdateRequest,
    AgentStoryboardAssetOptionsOut,
)
from app.schemas.agent_storyboard_media import (
    AgentEpisodeVideoGenerationOut,
    AgentEpisodeVideoGenerationRequest,
    AgentEpisodeVideosOut,
    AgentStoryboardPrimaryVideoOut,
    AgentStoryboardPrimaryVideoRequest,
    AgentStoryboardVideoGenerationRequest,
    AgentStoryboardVideoConfigOut,
    AgentStoryboardVideoConfigRequest,
    AgentStoryboardVideoVersionsOut,
)
from app.services.agent.storyboard_media import (
    get_agent_episode_videos,
    get_agent_storyboard_video_versions,
    select_agent_storyboard_primary_video,
    submit_agent_episode_videos,
    submit_agent_storyboard_video,
)
from app.services.agent.storyboards import (
    copy_agent_storyboard,
    create_agent_storyboard,
    delete_agent_storyboard,
    generate_agent_storyboards,
    get_agent_storyboards,
    get_agent_storyboard_asset_options,
    reorder_agent_storyboards,
    update_agent_storyboard_video_config,
    update_agent_storyboard,
)
from app.services.agent.workflow_steps import require_agent_step_access


async def require_storyboard_step(
    production_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await require_agent_step_access(db, production_id, current_user.id, 3)


router = APIRouter(dependencies=[Depends(require_storyboard_step)])


@router.get(
    "/agent-productions/{production_id}/episodes/{chapter_id}/storyboards/{storyboard_id}/asset-options"
)
async def my_agent_storyboard_asset_options(
    production_id: UUID,
    chapter_id: UUID,
    storyboard_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await get_agent_storyboard_asset_options(
        db,
        production_id,
        chapter_id,
        storyboard_id,
        current_user.id,
    )
    data = AgentStoryboardAssetOptionsOut.model_validate(result)
    return success(data=data.model_dump(mode="json"))


@router.put(
    "/agent-productions/{production_id}/episodes/{chapter_id}/storyboards/{storyboard_id}/video-config"
)
async def update_my_agent_storyboard_video_config(
    production_id: UUID,
    chapter_id: UUID,
    storyboard_id: UUID,
    payload: AgentStoryboardVideoConfigRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await update_agent_storyboard_video_config(
        db,
        production_id,
        chapter_id,
        storyboard_id,
        current_user,
        payload,
    )
    data = AgentStoryboardVideoConfigOut.model_validate(result)
    return success(data=data.model_dump(mode="json"), message="分镜组视频配置已更新")


@router.get(
    "/agent-productions/{production_id}/episodes/{chapter_id}/storyboards/{storyboard_id}/video-versions"
)
async def my_agent_storyboard_video_versions(
    production_id: UUID,
    chapter_id: UUID,
    storyboard_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await get_agent_storyboard_video_versions(
        db,
        production_id,
        chapter_id,
        storyboard_id,
        current_user.id,
    )
    data = AgentStoryboardVideoVersionsOut.model_validate(result)
    return success(data=data.model_dump(mode="json"))


@router.put(
    "/agent-productions/{production_id}/episodes/{chapter_id}/storyboards/{storyboard_id}/primary-video"
)
async def select_my_agent_storyboard_primary_video(
    production_id: UUID,
    chapter_id: UUID,
    storyboard_id: UUID,
    payload: AgentStoryboardPrimaryVideoRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await select_agent_storyboard_primary_video(
        db,
        production_id,
        chapter_id,
        storyboard_id,
        current_user,
        payload,
    )
    data = AgentStoryboardPrimaryVideoOut.model_validate(result)
    return success(data=data.model_dump(mode="json"), message="分镜组主视频已确认")


@router.get("/agent-productions/{production_id}/episodes/{chapter_id}/videos")
async def my_agent_episode_videos(
    production_id: UUID,
    chapter_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await get_agent_episode_videos(
        db,
        production_id,
        chapter_id,
        current_user.id,
    )
    data = AgentEpisodeVideosOut.model_validate(result)
    return success(data=data.model_dump(mode="json"))


@router.post(
    "/agent-productions/{production_id}/episodes/{chapter_id}/video-generations"
)
async def generate_my_agent_episode_videos(
    production_id: UUID,
    chapter_id: UUID,
    payload: AgentEpisodeVideoGenerationRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await submit_agent_episode_videos(
        db,
        production_id,
        chapter_id,
        current_user,
        payload,
    )
    data = AgentEpisodeVideoGenerationOut.model_validate(result)
    return success(data=data.model_dump(mode="json"), message="本集视频任务已提交")


@router.post(
    "/agent-productions/{production_id}/episodes/{chapter_id}/storyboards/{storyboard_id}/video-generations"
)
async def generate_my_agent_storyboard_video(
    production_id: UUID,
    chapter_id: UUID,
    storyboard_id: UUID,
    payload: AgentStoryboardVideoGenerationRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await submit_agent_storyboard_video(
        db,
        production_id,
        chapter_id,
        storyboard_id,
        current_user,
        payload,
    )
    data = AgentEpisodeVideoGenerationOut.model_validate(result)
    return success(data=data.model_dump(mode="json"), message="分镜组视频任务已提交")


@router.post(
    "/agent-productions/{production_id}/episodes/{chapter_id}/storyboards"
)
async def create_my_agent_storyboard(
    production_id: UUID,
    chapter_id: UUID,
    payload: AgentStoryboardCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await create_agent_storyboard(
        db,
        production_id,
        chapter_id,
        current_user,
        payload,
    )
    data = AgentStoryboardItemOut.model_validate(result)
    return success(data=data.model_dump(mode="json"), message="分镜组已创建")


@router.put(
    "/agent-productions/{production_id}/episodes/{chapter_id}/storyboards/order"
)
async def reorder_my_agent_storyboards(
    production_id: UUID,
    chapter_id: UUID,
    payload: AgentStoryboardReorderRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await reorder_agent_storyboards(
        db,
        production_id,
        chapter_id,
        current_user,
        payload,
    )
    data = AgentStoryboardOrderOut.model_validate(result)
    return success(data=data.model_dump(mode="json"), message="分镜组顺序已更新")


@router.patch("/agent-productions/{production_id}/storyboards/{storyboard_id}")
async def update_my_agent_storyboard(
    production_id: UUID,
    storyboard_id: UUID,
    payload: AgentStoryboardUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await update_agent_storyboard(
        db,
        production_id,
        storyboard_id,
        current_user,
        payload,
    )
    data = AgentStoryboardItemOut.model_validate(result)
    return success(data=data.model_dump(mode="json"), message="分镜组已更新")


@router.post("/agent-productions/{production_id}/storyboards/{storyboard_id}/copy")
async def copy_my_agent_storyboard(
    production_id: UUID,
    storyboard_id: UUID,
    payload: AgentStoryboardCopyRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await copy_agent_storyboard(
        db,
        production_id,
        storyboard_id,
        current_user,
        payload,
    )
    data = AgentStoryboardItemOut.model_validate(result)
    return success(data=data.model_dump(mode="json"), message="分镜组已复制")


@router.delete("/agent-productions/{production_id}/storyboards/{storyboard_id}")
async def delete_my_agent_storyboard(
    production_id: UUID,
    storyboard_id: UUID,
    expected_core_asset_lock_version: int = Query(ge=1),
    expected_revision: int = Query(ge=1),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await delete_agent_storyboard(
        db,
        production_id,
        storyboard_id,
        current_user,
        expected_core_asset_lock_version=expected_core_asset_lock_version,
        expected_revision=expected_revision,
    )
    data = AgentStoryboardDeleteOut.model_validate(result)
    return success(data=data.model_dump(mode="json"), message="分镜组已删除")


@router.get("/agent-productions/{production_id}/storyboards")
async def my_agent_storyboards(
    production_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await get_agent_storyboards(db, production_id, current_user.id)
    data = AgentStoryboardPackageOut.model_validate(result)
    return success(data=data.model_dump(mode="json"))


@router.post("/agent-productions/{production_id}/storyboards/generations")
async def generate_my_agent_storyboards(
    production_id: UUID,
    payload: AgentStoryboardGenerateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await generate_agent_storyboards(db, production_id, current_user, payload)
    data = AgentStoryboardPackageOut.model_validate(result)
    return success(data=data.model_dump(mode="json"), message="分镜脚本任务已提交")
