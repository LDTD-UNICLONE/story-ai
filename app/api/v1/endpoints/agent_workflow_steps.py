from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.responses import success
from app.db.session import get_db
from app.models.user import User
from app.schemas.agent_workflow import AgentWorkflowOut
from app.services.agent.workflow_steps import get_agent_workflow


router = APIRouter()


@router.get("/agent-productions/{production_id}/workflow")
async def my_agent_workflow(
    production_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await get_agent_workflow(db, production_id, current_user.id)
    data = AgentWorkflowOut.model_validate(result)
    return success(data=data.model_dump(mode="json"))
