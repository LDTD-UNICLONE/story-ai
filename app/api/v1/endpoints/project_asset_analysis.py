from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.responses import success
from app.db.session import get_db
from app.models.user import User
from app.schemas.project_asset import ProjectAssetAnalyzeOut, ProjectAssetAnalyzeRequest
from app.services.project_asset_analysis import submit_asset_analysis

router = APIRouter(prefix="/projects/{project_id}/chapters/{chapter_id}/asset-analyses")


@router.post("/{asset_type}")
async def analyze_project_assets(
    project_id: UUID,
    chapter_id: UUID,
    asset_type: Literal["character", "scene", "prop"],
    payload: ProjectAssetAnalyzeRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task_record, points_cost = await submit_asset_analysis(
        db,
        project_id=project_id,
        chapter_id=chapter_id,
        asset_type=asset_type,
        user=current_user,
        payload=payload,
    )
    data = ProjectAssetAnalyzeOut(
        task_record_id=task_record.id,
        asset_type=asset_type,
        status=task_record.status,
        points_cost=points_cost,
    )
    return success(data=data.model_dump(mode="json"), message="任务已提交")
