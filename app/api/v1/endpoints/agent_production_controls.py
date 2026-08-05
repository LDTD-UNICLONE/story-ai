from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.responses import success
from app.db.session import get_db
from app.models.user import User
from app.schemas.agent_production_control import (
    AgentJobActionOut,
    AgentJobRetryRequest,
    AgentJobSkipRequest,
    AgentProductionCostOut,
    AgentProductionEventListOut,
    AgentProductionEventOut,
    AgentProductionExceptionListOut,
    AgentProductionExceptionOut,
    AgentProductionMatrixOut,
)
from app.services.agent_batch_productions import retry_batch_jobs, skip_batch_jobs
from app.services.agent_production_monitoring import (
    get_agent_production_costs,
    get_agent_production_matrix,
    list_agent_production_events,
    list_agent_production_exceptions,
)

router = APIRouter()


@router.get("/agent-productions/{production_id}/matrix")
async def my_agent_production_matrix(
    production_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await get_agent_production_matrix(db, production_id, current_user.id)
    data = AgentProductionMatrixOut.model_validate(result)
    return success(data=data.model_dump(mode="json"))


@router.get("/agent-productions/{production_id}/exceptions")
async def my_agent_production_exceptions(
    production_id: UUID,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    items, total = await list_agent_production_exceptions(
        db,
        production_id,
        current_user.id,
        page=page,
        page_size=page_size,
    )
    data = AgentProductionExceptionListOut(
        items=[AgentProductionExceptionOut.model_validate(item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
    )
    return success(data=data.model_dump(mode="json"))


@router.get("/agent-productions/{production_id}/events")
async def my_agent_production_events(
    production_id: UUID,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    items, total = await list_agent_production_events(
        db,
        production_id,
        current_user.id,
        page=page,
        page_size=page_size,
    )
    data = AgentProductionEventListOut(
        items=[AgentProductionEventOut.model_validate(item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
    )
    return success(data=data.model_dump(mode="json"))


@router.get("/agent-productions/{production_id}/costs")
async def my_agent_production_costs(
    production_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await get_agent_production_costs(db, production_id, current_user.id)
    data = AgentProductionCostOut.model_validate(result)
    return success(data=data.model_dump(mode="json"))


@router.post("/agent-productions/{production_id}/jobs/retry")
async def retry_my_agent_production_jobs(
    production_id: UUID,
    payload: AgentJobRetryRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await retry_batch_jobs(db, production_id, current_user, payload)
    data = AgentJobActionOut.model_validate(result)
    return success(data=data.model_dump(mode="json"), message="指定任务已重新提交")


@router.post("/agent-productions/{production_id}/jobs/skip")
async def skip_my_agent_production_jobs(
    production_id: UUID,
    payload: AgentJobSkipRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await skip_batch_jobs(db, production_id, current_user, payload)
    data = AgentJobActionOut.model_validate(result)
    return success(data=data.model_dump(mode="json"), message="指定任务已人工处理")
