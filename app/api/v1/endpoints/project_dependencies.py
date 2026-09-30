from uuid import UUID

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.user import User
from app.services.projects.queries import get_project_or_404


async def require_standard_project(
    project_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    """限制普通项目路由只能访问当前用户的启用中普通项目。"""
    await get_project_or_404(db, project_id, current_user.id)
