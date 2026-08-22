from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.v1.endpoints.project_dependencies import require_standard_project
from app.api.v1.endpoints.task_polling import set_task_poll_headers, task_next_poll_seconds
from app.core.responses import success
from app.db.session import get_db
from app.models.user import User
from app.schemas.project_storyboard import (
    ProjectStoryboardAnalyzeOut,
    ProjectStoryboardAnalyzeRequest,
    ProjectStoryboardCreateRequest,
    ProjectStoryboardImageGenerateOut,
    ProjectStoryboardImageGenerateRequest,
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
from app.schemas.project_generated_asset import (
    ProjectGeneratedAssetListOut,
    ProjectGeneratedAssetOut,
    ProjectGeneratedAssetSelectOut,
    ProjectGeneratedAssetSelectRequest,
)
from app.services.project_generated_assets import (
    list_project_generated_asset_history,
    select_project_generated_asset_history,
)
from app.services.project_storyboards import (
    create_project_storyboard,
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
from app.services.project_storyboard_images import submit_storyboard_image_generation
from app.services.project_storyboard_videos import submit_storyboard_video_generation

router = APIRouter(
    prefix="/projects/{project_id}/chapters/{chapter_id}/storyboards",
    dependencies=[Depends(require_standard_project)],
)


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


@router.post("")
async def create_my_project_storyboard(
    project_id: UUID,
    chapter_id: UUID,
    payload: ProjectStoryboardCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    storyboard = await create_project_storyboard(
        db,
        project_id=project_id,
        chapter_id=chapter_id,
        user_id=current_user.id,
        payload=payload,
    )
    return success(
        data=ProjectStoryboardOut.model_validate(storyboard).model_dump(mode="json"),
        message="创建成功",
    )


@router.post("/analyze")
async def analyze_my_project_storyboards(
    project_id: UUID,
    chapter_id: UUID,
    payload: ProjectStoryboardAnalyzeRequest,
    response: Response,
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
        next_poll_seconds=task_next_poll_seconds(task_record),
    )
    set_task_poll_headers(response, data.next_poll_seconds)
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
    response: Response,
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
        next_poll_seconds=task_next_poll_seconds(task_record),
    )
    set_task_poll_headers(response, data.next_poll_seconds)
    return success(data=data.model_dump(mode="json"), message="任务已提交")


@router.post("/{storyboard_id}/storyboard-prompt-generation")
async def generate_my_project_storyboard_prompt(
    project_id: UUID,
    chapter_id: UUID,
    storyboard_id: UUID,
    payload: ProjectStoryboardPromptRequest,
    response: Response,
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
        next_poll_seconds=task_next_poll_seconds(task_record),
    )
    set_task_poll_headers(response, data.next_poll_seconds)
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


@router.get("/{storyboard_id}/generation-history")
async def my_project_storyboard_generation_history(
    project_id: UUID,
    chapter_id: UUID,
    storyboard_id: UUID,
    media_type: Literal["image", "video"] = Query(default="video"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await get_project_storyboard_or_404(
        db,
        project_id=project_id,
        chapter_id=chapter_id,
        storyboard_id=storyboard_id,
        user_id=current_user.id,
    )
    items, total = await list_project_generated_asset_history(
        db,
        project_id=project_id,
        user_id=current_user.id,
        target_type="storyboard",
        target_id=storyboard_id,
        media_type=media_type,
        page=page,
        page_size=page_size,
    )
    data = ProjectGeneratedAssetListOut(
        items=[ProjectGeneratedAssetOut.model_validate(item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
    )
    return success(data=data.model_dump(mode="json"))


@router.post("/{storyboard_id}/generation-history/{history_id:uuid}/select")
async def select_my_project_storyboard_generation_history(
    project_id: UUID,
    chapter_id: UUID,
    storyboard_id: UUID,
    history_id: UUID,
    payload: ProjectGeneratedAssetSelectRequest,
    media_type: Optional[Literal["image", "video"]] = Query(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await get_project_storyboard_or_404(
        db,
        project_id=project_id,
        chapter_id=chapter_id,
        storyboard_id=storyboard_id,
        user_id=current_user.id,
    )
    history, selected_url, last_frame_url = await select_project_generated_asset_history(
        db,
        project_id=project_id,
        user_id=current_user.id,
        history_id=history_id,
        target_type="storyboard",
        target_id=storyboard_id,
        media_type=media_type,
        result_url=payload.result_url,
    )
    data = ProjectGeneratedAssetSelectOut(
        history=ProjectGeneratedAssetOut.model_validate(history),
        selected_url=selected_url,
        last_frame_url=last_frame_url,
    )
    return success(data=data.model_dump(mode="json"), message="选择成功")


@router.post("/{storyboard_id}/image-generation")
async def generate_my_project_storyboard_image(
    project_id: UUID,
    chapter_id: UUID,
    storyboard_id: UUID,
    payload: ProjectStoryboardImageGenerateRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task_record, points_cost = await submit_storyboard_image_generation(
        db,
        project_id=project_id,
        chapter_id=chapter_id,
        storyboard_id=storyboard_id,
        user=current_user,
        payload=payload,
    )
    data = ProjectStoryboardImageGenerateOut(
        task_record_id=task_record.id,
        storyboard_id=storyboard_id,
        aspect_ratio=(task_record.extra or {}).get("aspect_ratio") or payload.aspect_ratio,
        status=task_record.status,
        points_cost=points_cost,
        next_poll_seconds=task_next_poll_seconds(task_record),
    )
    set_task_poll_headers(response, data.next_poll_seconds)
    return success(data=data.model_dump(mode="json"), message="任务已提交")


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
    response: Response,
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
        resolution=(task_record.extra or {}).get("resolution") or payload.resolution,
        return_last_frame=payload.return_last_frame,
        status=task_record.status,
        points_cost=points_cost,
        next_poll_seconds=task_next_poll_seconds(task_record),
    )
    set_task_poll_headers(response, data.next_poll_seconds)
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
    return success(
        data=ProjectStoryboardOut.model_validate(storyboard).model_dump(mode="json"),
        message="更新成功",
    )


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
    return success(
        data=ProjectStoryboardOut.model_validate(storyboard).model_dump(mode="json"),
        message="删除成功",
    )
