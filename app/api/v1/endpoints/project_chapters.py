from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.responses import success
from app.db.session import get_db
from app.models.user import User
from app.schemas.project_chapter import (
    ProjectChapterCreateRequest,
    ProjectChapterListOut,
    ProjectChapterOut,
    ProjectChapterUpdateRequest,
)
from app.services.project_chapters import (
    create_project_chapter,
    delete_project_chapter,
    get_project_chapter_or_404,
    list_project_chapters,
    update_project_chapter,
)

router = APIRouter(prefix="/projects/{project_id}/chapters")


@router.get("")
async def my_project_chapters(
    project_id: UUID,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    chapters, total = await list_project_chapters(
        db,
        project_id=project_id,
        user_id=current_user.id,
        page=page,
        page_size=page_size,
    )
    data = ProjectChapterListOut(
        items=[ProjectChapterOut.model_validate(item) for item in chapters],
        total=total,
        page=page,
        page_size=page_size,
    )
    return success(data=data.model_dump(mode="json"))


@router.post("")
async def create_my_project_chapter(
    project_id: UUID,
    payload: ProjectChapterCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    chapter = await create_project_chapter(db, project_id, current_user, payload)
    return success(
        data=ProjectChapterOut.model_validate(chapter).model_dump(mode="json"), message="创建成功"
    )


@router.get("/{chapter_id}")
async def my_project_chapter_detail(
    project_id: UUID,
    chapter_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    chapter = await get_project_chapter_or_404(db, project_id, chapter_id, current_user.id)
    return success(data=ProjectChapterOut.model_validate(chapter).model_dump(mode="json"))


@router.patch("/{chapter_id}")
async def update_my_project_chapter(
    project_id: UUID,
    chapter_id: UUID,
    payload: ProjectChapterUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    chapter = await update_project_chapter(db, project_id, chapter_id, current_user.id, payload)
    return success(
        data=ProjectChapterOut.model_validate(chapter).model_dump(mode="json"), message="更新成功"
    )


@router.delete("/{chapter_id}")
async def delete_my_project_chapter(
    project_id: UUID,
    chapter_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    chapter = await delete_project_chapter(db, project_id, chapter_id, current_user.id)
    return success(
        data=ProjectChapterOut.model_validate(chapter).model_dump(mode="json"), message="删除成功"
    )
