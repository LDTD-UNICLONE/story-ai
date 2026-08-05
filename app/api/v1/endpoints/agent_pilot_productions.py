from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.responses import success
from app.db.session import get_db
from app.models.user import User
from app.schemas.agent_pilot_production import (
    AgentPilotActionRequest,
    AgentPilotProductionOut,
)
from app.services.agent_pilot_productions import (
    confirm_pilot_production,
    get_pilot_production,
    start_pilot_storyboards,
    submit_pilot_media,
)

router = APIRouter()


@router.get("/agent-productions/{production_id}/pilot")
async def my_agent_pilot_production(
    production_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await get_pilot_production(db, production_id, current_user.id)
    data = AgentPilotProductionOut.model_validate(result)
    return success(data=data.model_dump(mode="json"))


@router.post("/agent-productions/{production_id}/pilot/storyboards")
async def start_my_agent_pilot_storyboards(
    production_id: UUID,
    payload: AgentPilotActionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await start_pilot_storyboards(db, production_id, current_user, payload)
    data = AgentPilotProductionOut.model_validate(result)
    return success(data=data.model_dump(mode="json"), message="试播集分镜任务已提交")


@router.post("/agent-productions/{production_id}/pilot/images")
async def generate_my_agent_pilot_images(
    production_id: UUID,
    payload: AgentPilotActionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await submit_pilot_media(db, production_id, current_user, payload, "image")
    data = AgentPilotProductionOut.model_validate(result)
    return success(data=data.model_dump(mode="json"), message="试播集故事板图片任务已提交")


@router.post("/agent-productions/{production_id}/pilot/videos")
async def generate_my_agent_pilot_videos(
    production_id: UUID,
    payload: AgentPilotActionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await submit_pilot_media(db, production_id, current_user, payload, "video")
    data = AgentPilotProductionOut.model_validate(result)
    return success(data=data.model_dump(mode="json"), message="试播集视频任务已提交")


@router.post("/agent-productions/{production_id}/pilot/confirm")
async def confirm_my_agent_pilot_production(
    production_id: UUID,
    payload: AgentPilotActionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await confirm_pilot_production(db, production_id, current_user, payload)
    data = AgentPilotProductionOut.model_validate(result)
    return success(data=data.model_dump(mode="json"), message="试播集已确认")
