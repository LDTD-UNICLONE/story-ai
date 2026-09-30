"""Read projects with explicit standard-project and internal-generation access rules."""

from typing import List, Optional, Tuple
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.exceptions import AppException
from app.models.project import Project


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
