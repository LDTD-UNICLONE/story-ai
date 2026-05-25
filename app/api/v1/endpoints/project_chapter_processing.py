from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.responses import success
from app.db.session import get_db
from app.models.user import User
from app.schemas.project_chapter import (
    ProjectChapterOut,
    ProjectChapterProcessOut,
    ProjectChapterProcessRequest,
)
from app.services.project_chapter_processing import submit_project_chapter_processing

router = APIRouter(prefix="/projects/{project_id}/chapters/{chapter_id}/processing")


@router.post("")
async def process_my_project_chapter(
    project_id: UUID,
    chapter_id: UUID,
    payload: ProjectChapterProcessRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    chapter, task_record, points_cost = await submit_project_chapter_processing(
        db,
        project_id=project_id,
        chapter_id=chapter_id,
        user=current_user,
        payload=payload,
    )
    data = ProjectChapterProcessOut(
        chapter=ProjectChapterOut.model_validate(chapter),
        task_record_id=task_record.id,
        points_cost=points_cost,
    )
    return success(data=data.model_dump(mode="json"), message="任务已提交")
