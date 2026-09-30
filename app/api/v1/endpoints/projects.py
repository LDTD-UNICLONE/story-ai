from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.responses import success
from app.db.session import get_db
from app.models.user import User
from app.schemas.project import (
    ProjectCreateRequest,
    ProjectListOut,
    ProjectOut,
    ProjectUpdateRequest,
)
from app.services.projects.queries import (
    get_project_or_404,
    list_projects,
)
from app.services.projects.lifecycle import (
    create_project,
    delete_project,
    update_project,
)

router = APIRouter(prefix="/projects")


@router.get("")
async def my_projects(
    keyword: Optional[str] = Query(default=None, max_length=64),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    projects, total = await list_projects(
        db,
        user_id=current_user.id,
        keyword=keyword,
        page=page,
        page_size=page_size,
    )
    data = ProjectListOut(
        items=[ProjectOut.model_validate(item) for item in projects],
        total=total,
        page=page,
        page_size=page_size,
    )
    return success(data=data.model_dump(mode="json"))


@router.post("")
async def create_my_project(
    payload: ProjectCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    project = await create_project(db, current_user, payload)
    return success(
        data=ProjectOut.model_validate(project).model_dump(mode="json"), message="创建成功"
    )


@router.get("/{project_id}")
async def my_project_detail(
    project_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    project = await get_project_or_404(db, project_id, current_user.id)
    return success(data=ProjectOut.model_validate(project).model_dump(mode="json"))


@router.patch("/{project_id}")
async def update_my_project(
    project_id: UUID,
    payload: ProjectUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    project = await update_project(db, project_id, current_user.id, payload)
    return success(
        data=ProjectOut.model_validate(project).model_dump(mode="json"), message="更新成功"
    )


@router.delete("/{project_id}")
async def delete_my_project(
    project_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    project = await delete_project(db, project_id, current_user.id)
    return success(
        data=ProjectOut.model_validate(project).model_dump(mode="json"), message="删除成功"
    )
