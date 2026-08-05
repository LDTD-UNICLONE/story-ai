from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.responses import success
from app.db.session import get_db
from app.models.user import User
from app.schemas.agent_review import (
    AgentDeliveryCreateRequest,
    AgentDeliveryListOut,
    AgentDeliveryOut,
    AgentDeliveryReadinessOut,
    AgentEpisodeApprovalOut,
    AgentEpisodeApproveRequest,
    AgentEpisodeVideoTimelineOut,
    AgentJianyingExportCreateRequest,
    AgentJianyingExportOut,
    AgentProductionReviewOut,
    AgentReviewIssueCreateRequest,
    AgentReviewIssueOut,
    AgentReviewIssueUpdateRequest,
)
from app.services.agent_reviews import (
    approve_agent_episode,
    create_agent_delivery,
    create_agent_jianying_export,
    create_agent_review_issue,
    get_agent_delivery_readiness,
    get_agent_delivery,
    get_agent_episode_video_timeline,
    get_agent_jianying_export,
    get_agent_production_review,
    list_agent_deliveries,
    list_agent_jianying_exports,
    update_agent_review_issue,
)
from app.services.agent_workflow_steps import require_agent_step_access


async def require_video_editing_step(
    production_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await require_agent_step_access(db, production_id, current_user.id, 4)


router = APIRouter(dependencies=[Depends(require_video_editing_step)])


@router.get("/agent-productions/{production_id}/review")
async def my_agent_production_review(
    production_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    review = await get_agent_production_review(db, production_id, current_user.id)
    data = AgentProductionReviewOut.model_validate(review)
    return success(data=data.model_dump(mode="json"))


@router.post("/agent-productions/{production_id}/review-issues")
async def create_my_agent_review_issue(
    production_id: UUID,
    payload: AgentReviewIssueCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    issue = await create_agent_review_issue(db, production_id, current_user, payload)
    data = AgentReviewIssueOut.model_validate(issue)
    return success(data=data.model_dump(mode="json"), message="审片问题已记录")


@router.patch("/agent-productions/{production_id}/review-issues/{issue_id}")
async def update_my_agent_review_issue(
    production_id: UUID,
    issue_id: UUID,
    payload: AgentReviewIssueUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    issue = await update_agent_review_issue(db, production_id, issue_id, current_user, payload)
    data = AgentReviewIssueOut.model_validate(issue)
    return success(data=data.model_dump(mode="json"), message="审片问题已处理")


@router.get(
    "/agent-productions/{production_id}/episodes/{chapter_id}/video-timeline"
)
async def my_agent_episode_video_timeline(
    production_id: UUID,
    chapter_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    timeline = await get_agent_episode_video_timeline(
        db, production_id, chapter_id, current_user.id
    )
    data = AgentEpisodeVideoTimelineOut.model_validate(timeline)
    return success(data=data.model_dump(mode="json"))


@router.post("/agent-productions/{production_id}/jianying-exports")
async def create_my_agent_jianying_export(
    production_id: UUID,
    payload: AgentJianyingExportCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    export = await create_agent_jianying_export(db, production_id, current_user, payload)
    data = AgentJianyingExportOut.model_validate(export)
    return success(data=data.model_dump(mode="json"), message="剪映草稿导出任务已创建")


@router.get("/agent-productions/{production_id}/jianying-exports")
async def my_agent_jianying_exports(
    production_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    items = await list_agent_jianying_exports(db, production_id, current_user.id)
    data = [AgentJianyingExportOut.model_validate(item) for item in items]
    return success(data=[item.model_dump(mode="json") for item in data])


@router.get("/agent-productions/{production_id}/jianying-exports/{export_id}")
async def my_agent_jianying_export(
    production_id: UUID,
    export_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    export = await get_agent_jianying_export(
        db, production_id, export_id, current_user.id
    )
    data = AgentJianyingExportOut.model_validate(export)
    return success(data=data.model_dump(mode="json"))


@router.post("/agent-productions/{production_id}/episodes/{chapter_id}/approve")
async def approve_my_agent_episode(
    production_id: UUID,
    chapter_id: UUID,
    payload: AgentEpisodeApproveRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    review = await approve_agent_episode(db, production_id, chapter_id, current_user, payload)
    data = AgentEpisodeApprovalOut.model_validate(review)
    return success(data=data.model_dump(mode="json"), message="剧集审片已确认")


@router.get("/agent-productions/{production_id}/delivery-readiness")
async def my_agent_delivery_readiness(
    production_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    readiness = await get_agent_delivery_readiness(db, production_id, current_user.id)
    data = AgentDeliveryReadinessOut.model_validate(readiness)
    return success(data=data.model_dump(mode="json"))


@router.post("/agent-productions/{production_id}/deliveries")
async def create_my_agent_delivery(
    production_id: UUID,
    payload: AgentDeliveryCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    delivery = await create_agent_delivery(db, production_id, current_user, payload)
    data = AgentDeliveryOut.model_validate(delivery)
    return success(data=data.model_dump(mode="json"), message="交付任务已创建")


@router.get("/agent-productions/{production_id}/deliveries")
async def my_agent_deliveries(
    production_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    items = await list_agent_deliveries(db, production_id, current_user.id)
    data = AgentDeliveryListOut(
        items=[AgentDeliveryOut.model_validate(item) for item in items]
    )
    return success(data=data.model_dump(mode="json"))


@router.get("/agent-productions/{production_id}/deliveries/{delivery_id}")
async def my_agent_delivery(
    production_id: UUID,
    delivery_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    delivery = await get_agent_delivery(db, production_id, delivery_id, current_user.id)
    data = AgentDeliveryOut.model_validate(delivery)
    return success(data=data.model_dump(mode="json"))
