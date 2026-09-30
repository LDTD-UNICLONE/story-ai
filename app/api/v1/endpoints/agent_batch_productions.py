from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.responses import success
from app.db.session import get_db
from app.models.user import User
from app.schemas.agent_batch_production import (
    AgentBatchDispatchRequest,
    AgentBatchProductionOut,
    AgentVideoModelSelectionRequest,
)
from app.services.agent.batch_productions import (
    dispatch_batch_production,
    get_batch_production,
    select_batch_video_model,
)
from app.services.agent.workflow_steps import require_agent_step_access

async def require_storyboard_generation_step(
    production_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await require_agent_step_access(db, production_id, current_user.id, 3)


router = APIRouter(dependencies=[Depends(require_storyboard_generation_step)])


@router.get("/agent-productions/{production_id}/batch")
async def my_agent_batch_production(
    production_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await get_batch_production(db, production_id, current_user.id)
    data = AgentBatchProductionOut.model_validate(result)
    return success(data=data.model_dump(mode="json"))


@router.post("/agent-productions/{production_id}/batch/dispatch")
async def dispatch_my_agent_batch_production(
    production_id: UUID,
    payload: AgentBatchDispatchRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await dispatch_batch_production(db, production_id, current_user, payload)
    data = AgentBatchProductionOut.model_validate(result)
    return success(data=data.model_dump(mode="json"), message="批量生产任务已补充")


@router.post("/agent-productions/{production_id}/batch/video-model")
async def select_my_agent_batch_video_model(
    production_id: UUID,
    payload: AgentVideoModelSelectionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await select_batch_video_model(db, production_id, current_user, payload)
    data = AgentBatchProductionOut.model_validate(result)
    return success(data=data.model_dump(mode="json"), message="视频模型已确认")
