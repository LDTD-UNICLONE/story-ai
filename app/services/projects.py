from typing import List, Optional, Tuple
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.exceptions import AppException
from app.core.timezone import beijing_datetime
from app.models.project import Project
from app.models.user import User
from app.schemas.project import ProjectCreateRequest, ProjectUpdateRequest
from app.services.styles import get_enabled_style_or_404


async def list_projects(
    db: AsyncSession,
    user_id: UUID,
    keyword: Optional[str],
    page: int,
    page_size: int,
) -> Tuple[List[Project], int]:
    conditions = [
        Project.user_id == user_id,
        Project.is_enabled.is_(True),
        Project.project_kind == "standard",
    ]
    if keyword:
        pattern = f"%{keyword}%"
        conditions.append(or_(Project.name.ilike(pattern), Project.description.ilike(pattern)))

    count_result = await db.execute(select(func.count()).select_from(Project).where(*conditions))
    total = count_result.scalar_one()

    result = await db.execute(
        select(Project)
        .options(selectinload(Project.style))
        .where(*conditions)
        .order_by(Project.created_at.desc(), Project.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(result.scalars().all()), total


async def get_project_or_404(db: AsyncSession, project_id: UUID, user_id: UUID) -> Project:
    result = await db.execute(
        select(Project)
        .where(
            Project.id == project_id,
            Project.user_id == user_id,
            Project.is_enabled.is_(True),
            Project.project_kind == "standard",
        )
        .options(selectinload(Project.style))
    )
    project = result.scalar_one_or_none()
    if project is None:
        raise AppException("项目不存在", code=40407, status_code=404)
    return project


async def get_owned_enabled_project_with_style_or_404(
    db: AsyncSession,
    project_id: UUID,
    user_id: UUID,
) -> Project:
    """读取当前用户的启用项目及其可用风格，供普通项目与 Agent 内部流程复用。"""
    result = await db.execute(
        select(Project)
        .where(
            Project.id == project_id,
            Project.user_id == user_id,
            Project.is_enabled.is_(True),
        )
        .options(selectinload(Project.style))
    )
    project = result.scalar_one_or_none()
    if project is None:
        raise AppException("项目不存在", code=40407, status_code=404)
    if project.style is None or not project.style.is_enabled:
        raise AppException("项目风格不存在或未启用", code=40403, status_code=404)
    return project


async def cancel_project_task_records(
    db: AsyncSession,
    project_id: UUID,
    user_id: UUID,
    *,
    reason: str,
) -> int:
    # 延迟导入避免 projects -> task_records -> project_generated_assets -> projects 循环依赖。
    from app.services.task_records import cancel_project_task_records as cancel_records

    return await cancel_records(db, project_id, user_id, reason=reason)


async def create_project(db: AsyncSession, user: User, payload: ProjectCreateRequest) -> Project:
    await get_enabled_style_or_404(db, payload.style_id)
    project = Project(
        user_id=user.id,
        style_id=payload.style_id,
        name=payload.name,
        cover=payload.cover or "",
        description=payload.description or "",
        generation_ratio=payload.generation_ratio,
        project_kind="standard",
        is_enabled=True,
    )
    db.add(project)
    await db.commit()
    return await get_project_or_404(db, project.id, user.id)


async def update_project(
    db: AsyncSession,
    project_id: UUID,
    user_id: UUID,
    payload: ProjectUpdateRequest,
) -> Project:
    project = await get_project_or_404(db, project_id, user_id)
    update_data = payload.model_dump(exclude_unset=True)
    if "style_id" in update_data:
        await get_enabled_style_or_404(db, update_data["style_id"])

    for field, value in update_data.items():
        setattr(project, field, value)
    project.updated_at = beijing_datetime()

    await db.commit()
    return await get_project_or_404(db, project_id, user_id)


async def delete_project(db: AsyncSession, project_id: UUID, user_id: UUID) -> Project:
    project = await get_project_or_404(db, project_id, user_id)
    project.is_enabled = False
    project.updated_at = beijing_datetime()
    await cancel_project_task_records(db, project_id, user_id, reason="项目已删除")
    await db.commit()
    return project
