from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.responses import success
from app.db.session import get_db
from app.models.user import User
from app.schemas.project_storyboard import (
    ProjectStoryboardAnalyzeOut,
    ProjectStoryboardAnalyzeRequest,
    ProjectStoryboardListOut,
    ProjectStoryboardMergeRequest,
    ProjectStoryboardOut,
    ProjectStoryboardPromptRequest,
    ProjectStoryboardRefineRequest,
    ProjectStoryboardSplitRequest,
    ProjectStoryboardUpdateRequest,
    ProjectStoryboardVideoGenerateOut,
    ProjectStoryboardVideoGenerateRequest,
)
from app.services.project_storyboards import (
    delete_project_storyboard,
    get_project_storyboard_or_404,
    list_project_storyboards,
    merge_project_storyboards,
    split_project_storyboard,
    submit_storyboard_analysis,
    submit_storyboard_image_prompt_generation,
    submit_storyboard_refinement,
    update_project_storyboard,
)
from app.services.project_storyboard_videos import submit_storyboard_video_generation

router = APIRouter(prefix="/projects/{project_id}/chapters/{chapter_id}/storyboards")


@router.get("")
async def my_project_storyboards(
    project_id: UUID,
    chapter_id: UUID,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    storyboards, total = await list_project_storyboards(
        db,
        project_id=project_id,
        chapter_id=chapter_id,
        user_id=current_user.id,
        page=page,
        page_size=page_size,
    )
    data = ProjectStoryboardListOut(
        items=[ProjectStoryboardOut.model_validate(item) for item in storyboards],
        total=total,
        page=page,
        page_size=page_size,
    )
    return success(data=data.model_dump(mode="json"))


@router.post("/analyze")
async def analyze_my_project_storyboards(
    project_id: UUID,
    chapter_id: UUID,
    payload: ProjectStoryboardAnalyzeRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task_record, points_cost = await submit_storyboard_analysis(
        db,
        project_id=project_id,
        chapter_id=chapter_id,
        user=current_user,
        payload=payload,
    )
    data = ProjectStoryboardAnalyzeOut(
        task_record_id=task_record.id,
        status=task_record.status,
        points_cost=points_cost,
    )
    return success(data=data.model_dump(mode="json"), message="任务已提交")


@router.post("/merge")
async def merge_my_project_storyboards(
    project_id: UUID,
    chapter_id: UUID,
    payload: ProjectStoryboardMergeRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    storyboards = await merge_project_storyboards(
        db,
        project_id=project_id,
        chapter_id=chapter_id,
        user_id=current_user.id,
        payload=payload,
    )
    data = ProjectStoryboardListOut(
        items=[ProjectStoryboardOut.model_validate(item) for item in storyboards],
        total=len(storyboards),
        page=1,
        page_size=len(storyboards),
    )
    return success(data=data.model_dump(mode="json"), message="合并成功")


@router.post("/{storyboard_id}/refine")
async def refine_my_project_storyboard(
    project_id: UUID,
    chapter_id: UUID,
    storyboard_id: UUID,
    payload: ProjectStoryboardRefineRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task_record, points_cost = await submit_storyboard_refinement(
        db,
        project_id=project_id,
        chapter_id=chapter_id,
        storyboard_id=storyboard_id,
        user=current_user,
        payload=payload,
    )
    data = ProjectStoryboardAnalyzeOut(
        task_record_id=task_record.id,
        status=task_record.status,
        points_cost=points_cost,
    )
    return success(data=data.model_dump(mode="json"), message="任务已提交")


@router.post("/{storyboard_id}/storyboard-prompt-generation")
async def generate_my_project_storyboard_prompt(
    project_id: UUID,
    chapter_id: UUID,
    storyboard_id: UUID,
    payload: ProjectStoryboardPromptRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task_record, points_cost = await submit_storyboard_image_prompt_generation(
        db,
        project_id=project_id,
        chapter_id=chapter_id,
        storyboard_id=storyboard_id,
        user=current_user,
        payload=payload,
    )
    data = ProjectStoryboardAnalyzeOut(
        task_record_id=task_record.id,
        status=task_record.status,
        points_cost=points_cost,
    )
    return success(data=data.model_dump(mode="json"), message="任务已提交")


@router.get("/{storyboard_id}")
async def my_project_storyboard_detail(
    project_id: UUID,
    chapter_id: UUID,
    storyboard_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    storyboard = await get_project_storyboard_or_404(
        db,
        project_id=project_id,
        chapter_id=chapter_id,
        storyboard_id=storyboard_id,
        user_id=current_user.id,
    )
    return success(data=ProjectStoryboardOut.model_validate(storyboard).model_dump(mode="json"))


@router.post("/{storyboard_id}/split")
async def split_my_project_storyboard(
    project_id: UUID,
    chapter_id: UUID,
    storyboard_id: UUID,
    payload: ProjectStoryboardSplitRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    storyboards = await split_project_storyboard(
        db,
        project_id=project_id,
        chapter_id=chapter_id,
        storyboard_id=storyboard_id,
        user_id=current_user.id,
        payload=payload,
    )
    data = ProjectStoryboardListOut(
        items=[ProjectStoryboardOut.model_validate(item) for item in storyboards],
        total=len(storyboards),
        page=1,
        page_size=len(storyboards),
    )
    return success(data=data.model_dump(mode="json"), message="拆分成功")


@router.post("/{storyboard_id}/video-generation")
async def generate_my_project_storyboard_video(
    project_id: UUID,
    chapter_id: UUID,
    storyboard_id: UUID,
    payload: ProjectStoryboardVideoGenerateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task_record, points_cost = await submit_storyboard_video_generation(
        db,
        project_id=project_id,
        chapter_id=chapter_id,
        storyboard_id=storyboard_id,
        user=current_user,
        payload=payload,
    )
    data = ProjectStoryboardVideoGenerateOut(
        task_record_id=task_record.id,
        storyboard_id=storyboard_id,
        generation_mode=payload.generation_mode,
        status=task_record.status,
        points_cost=points_cost,
    )
    return success(data=data.model_dump(mode="json"), message="任务已提交")


@router.patch("/{storyboard_id}")
async def update_my_project_storyboard(
    project_id: UUID,
    chapter_id: UUID,
    storyboard_id: UUID,
    payload: ProjectStoryboardUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    storyboard = await update_project_storyboard(
        db,
        project_id=project_id,
        chapter_id=chapter_id,
        storyboard_id=storyboard_id,
        user_id=current_user.id,
        payload=payload,
    )
    return success(data=ProjectStoryboardOut.model_validate(storyboard).model_dump(mode="json"), message="更新成功")


@router.delete("/{storyboard_id}")
async def delete_my_project_storyboard(
    project_id: UUID,
    chapter_id: UUID,
    storyboard_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    storyboard = await delete_project_storyboard(
        db,
        project_id=project_id,
        chapter_id=chapter_id,
        storyboard_id=storyboard_id,
        user_id=current_user.id,
    )
    return success(data=ProjectStoryboardOut.model_validate(storyboard).model_dump(mode="json"), message="删除成功")
