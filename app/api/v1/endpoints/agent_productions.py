from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.responses import success
from app.db.session import get_db
from app.models.user import User
from app.schemas.agent_production import (
    AgentProductionCreatedOut,
    AgentProductionConfigurationOut,
    AgentProductionConfigurationRequest,
    AgentProductionDeletedOut,
    AgentProductionDetailOut,
    AgentProductionFromTextRequest,
    AgentProductionMode,
    AgentProductionProjectListOut,
    AgentProductionSummaryOut,
    AgentProductionStatus,
    AgentVideoResolution,
    AgentEpisodePlanConfirmOut,
    AgentEpisodePlanConfirmRequest,
    AgentEpisodePlanImpactOut,
    AgentEpisodePlanListOut,
    AgentEpisodePlanMergeRequest,
    AgentEpisodePlanSplitRequest,
    AgentEpisodePlanUpdateRequest,
)
from app.schemas.project import ProjectGenerationRatio
from app.schemas.agent_production_control import AgentProductionWorkbenchOut
from app.services.agent.episode_plans import (
    confirm_episode_plan,
    get_episode_plan_document,
    merge_episode_plans,
    preview_episode_plan_impact,
    split_episode_plan,
    update_episode_plan,
)
from app.services.agent.entries import (
    configure_agent_production,
    create_agent_production_from_file,
    create_agent_production_from_text,
    get_agent_production_configuration,
    list_agent_projects,
)
from app.services.agent.productions import (
    apply_agent_production_action,
    delete_agent_production,
    get_agent_production_or_404,
)
from app.services.agent.workbench import get_agent_production_workbench

router = APIRouter()


@router.post("/agent-productions/from-file")
async def create_my_agent_production_from_file(
    file: UploadFile = File(...),
    name: Optional[str] = Form(default=None, max_length=128),
    style_id: UUID = Form(...),
    generation_ratio: ProjectGenerationRatio = Form(...),
    video_resolution: AgentVideoResolution = Form(...),
    mode: AgentProductionMode = Form(...),
    video_model_id: Optional[UUID] = Form(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await create_agent_production_from_file(
        db,
        current_user,
        file,
        name=name,
        style_id=style_id,
        generation_ratio=generation_ratio,
        video_resolution=video_resolution,
        mode=mode,
        video_model_id=video_model_id,
    )
    data = AgentProductionCreatedOut.model_validate(result)
    return success(data=data.model_dump(mode="json"), message="创建成功")


@router.get("/agent-productions/{production_id}/configuration")
async def my_agent_production_configuration(
    production_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await get_agent_production_configuration(db, production_id, current_user.id)
    data = AgentProductionConfigurationOut.model_validate(result)
    return success(data=data.model_dump(mode="json"))


@router.put("/agent-productions/{production_id}/configuration")
async def configure_my_agent_production(
    production_id: UUID,
    payload: AgentProductionConfigurationRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await configure_agent_production(
        db,
        production_id,
        current_user.id,
        payload,
    )
    data = AgentProductionConfigurationOut.model_validate(result)
    return success(data=data.model_dump(mode="json"), message="整体配置已保存")


@router.post("/agent-productions/from-text")
async def create_my_agent_production_from_text(
    payload: AgentProductionFromTextRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await create_agent_production_from_text(db, current_user, payload)
    data = AgentProductionCreatedOut.model_validate(result)
    return success(data=data.model_dump(mode="json"), message="创建成功")


@router.get("/agent-productions")
async def my_agent_projects(
    status: Optional[AgentProductionStatus] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    productions, total = await list_agent_projects(
        db,
        user_id=current_user.id,
        status=status,
        page=page,
        page_size=page_size,
    )
    data = AgentProductionProjectListOut(
        items=[AgentProductionSummaryOut.model_validate(item) for item in productions],
        total=total,
        page=page,
        page_size=page_size,
    )
    return success(data=data.model_dump(mode="json"))


@router.get("/agent-productions/{production_id}/episode-plans")
async def my_agent_episode_plans(
    production_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    document = await get_episode_plan_document(db, production_id, current_user.id)
    data = AgentEpisodePlanListOut.model_validate(document)
    return success(data=data.model_dump(mode="json"))


@router.patch("/agent-productions/{production_id}/episode-plans/{plan_id}")
async def update_my_agent_episode_plan(
    production_id: UUID,
    plan_id: UUID,
    payload: AgentEpisodePlanUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    document = await update_episode_plan(db, production_id, plan_id, current_user, payload)
    data = AgentEpisodePlanListOut.model_validate(document)
    return success(data=data.model_dump(mode="json"), message="分集规划已更新")


@router.post("/agent-productions/{production_id}/episode-plans/merge")
async def merge_my_agent_episode_plans(
    production_id: UUID,
    payload: AgentEpisodePlanMergeRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    document = await merge_episode_plans(db, production_id, current_user, payload)
    data = AgentEpisodePlanListOut.model_validate(document)
    return success(data=data.model_dump(mode="json"), message="相邻剧集已合并")


@router.post("/agent-productions/{production_id}/episode-plans/{plan_id}/split")
async def split_my_agent_episode_plan(
    production_id: UUID,
    plan_id: UUID,
    payload: AgentEpisodePlanSplitRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    document = await split_episode_plan(db, production_id, plan_id, current_user, payload)
    data = AgentEpisodePlanListOut.model_validate(document)
    return success(data=data.model_dump(mode="json"), message="剧集已拆分")


@router.get("/agent-productions/{production_id}/episode-plans/impact-preview")
async def my_agent_episode_plan_impact_preview(
    production_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    impact = await preview_episode_plan_impact(db, production_id, current_user.id)
    data = AgentEpisodePlanImpactOut.model_validate(impact)
    return success(data=data.model_dump(mode="json"))


@router.post("/agent-productions/{production_id}/episode-plans/confirm")
async def confirm_my_agent_episode_plan(
    production_id: UUID,
    payload: AgentEpisodePlanConfirmRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await confirm_episode_plan(db, production_id, current_user, payload)
    data = AgentEpisodePlanConfirmOut.model_validate(result)
    return success(data=data.model_dump(mode="json"), message="分集规划已确认")


@router.get("/agent-productions/{production_id}")
async def my_agent_production_detail(
    production_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    production = await get_agent_production_or_404(db, production_id, current_user.id)
    data = AgentProductionDetailOut.model_validate(production)
    return success(data=data.model_dump(mode="json"))


@router.delete("/agent-productions/{production_id}")
async def delete_my_agent_production(
    production_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await delete_agent_production(db, production_id, current_user)
    data = AgentProductionDeletedOut.model_validate(result)
    return success(data=data.model_dump(mode="json"), message="删除成功")


@router.get("/agent-productions/{production_id}/workbench")
async def my_agent_production_workbench(
    production_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    workbench = await get_agent_production_workbench(db, production_id, current_user.id)
    data = AgentProductionWorkbenchOut.model_validate(workbench)
    return success(data=data.model_dump(mode="json"))


@router.post("/agent-productions/{production_id}/start")
async def start_my_agent_production(
    production_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    production = await apply_agent_production_action(db, production_id, current_user, "start")
    data = AgentProductionDetailOut.model_validate(production)
    return success(data=data.model_dump(mode="json"), message="任务已启动")


@router.post("/agent-productions/{production_id}/pause")
async def pause_my_agent_production(
    production_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    production = await apply_agent_production_action(db, production_id, current_user, "pause")
    data = AgentProductionDetailOut.model_validate(production)
    return success(data=data.model_dump(mode="json"), message="任务已暂停")


@router.post("/agent-productions/{production_id}/resume")
async def resume_my_agent_production(
    production_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    production = await apply_agent_production_action(db, production_id, current_user, "resume")
    data = AgentProductionDetailOut.model_validate(production)
    return success(data=data.model_dump(mode="json"), message="任务已恢复")


@router.post("/agent-productions/{production_id}/cancel")
async def cancel_my_agent_production(
    production_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    production = await apply_agent_production_action(db, production_id, current_user, "cancel")
    data = AgentProductionDetailOut.model_validate(production)
    return success(data=data.model_dump(mode="json"), message="任务已取消")
