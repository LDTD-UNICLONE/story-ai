"""Create, update and delete standard projects and cancel their tasks on deletion."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import beijing_datetime
from app.models.project import Project
from app.models.user import User
from app.schemas.project import ProjectCreateRequest, ProjectUpdateRequest
from app.services.projects.queries import get_project_or_404
from app.services.generation.task_records import cancel_project_task_records


async def create_project(db: AsyncSession, user: User, payload: ProjectCreateRequest) -> Project:
    project = Project(
        user_id=user.id,
        name=payload.name,
        cover=payload.cover or "",
        description=payload.description or "",
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
