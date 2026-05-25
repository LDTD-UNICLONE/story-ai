from typing import List, Optional, Tuple
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException
from app.core.timezone import beijing_datetime
from app.models.project_chapter import ProjectChapter
from app.models.user import User
from app.schemas.project_chapter import (
    ProjectChapterCreateRequest,
    ProjectChapterUpdateRequest,
)
from app.services.projects import get_project_or_404


async def list_project_chapters(
    db: AsyncSession,
    project_id: UUID,
    user_id: UUID,
    page: int,
    page_size: int,
) -> Tuple[List[ProjectChapter], int]:
    await get_project_or_404(db, project_id, user_id)
    conditions = [
        ProjectChapter.project_id == project_id,
        ProjectChapter.user_id == user_id,
        ProjectChapter.is_enabled.is_(True),
    ]

    count_result = await db.execute(
        select(func.count()).select_from(ProjectChapter).where(*conditions)
    )
    total = count_result.scalar_one()

    result = await db.execute(
        select(ProjectChapter)
        .where(*conditions)
        .order_by(ProjectChapter.sort_order.asc(), ProjectChapter.created_at.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(result.scalars().all()), total


async def get_project_chapter_or_404(
    db: AsyncSession,
    project_id: UUID,
    chapter_id: UUID,
    user_id: UUID,
) -> ProjectChapter:
    result = await db.execute(
        select(ProjectChapter).where(
            ProjectChapter.id == chapter_id,
            ProjectChapter.project_id == project_id,
            ProjectChapter.user_id == user_id,
            ProjectChapter.is_enabled.is_(True),
        )
    )
    chapter = result.scalar_one_or_none()
    if chapter is None:
        raise AppException("项目章节不存在", code=40408, status_code=404)
    return chapter


async def create_project_chapter(
    db: AsyncSession,
    project_id: UUID,
    user: User,
    payload: ProjectChapterCreateRequest,
) -> ProjectChapter:
    await get_project_or_404(db, project_id, user.id)
    chapter = ProjectChapter(
        project_id=project_id,
        user_id=user.id,
        title=payload.title,
        content=payload.content,
        processing_prompt=payload.processing_prompt,
        sort_order=payload.sort_order,
        process_status="draft",
        extra={},
        is_enabled=True,
    )
    db.add(chapter)
    await db.commit()
    await db.refresh(chapter)
    return chapter


async def update_project_chapter(
    db: AsyncSession,
    project_id: UUID,
    chapter_id: UUID,
    user_id: UUID,
    payload: ProjectChapterUpdateRequest,
) -> ProjectChapter:
    chapter = await get_project_chapter_or_404(db, project_id, chapter_id, user_id)
    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(chapter, field, value)
    if {"content", "processing_prompt"} & set(update_data.keys()):
        chapter.process_status = "draft"
        chapter.processed_content = None
    chapter.updated_at = beijing_datetime()
    await db.commit()
    await db.refresh(chapter)
    return chapter


async def delete_project_chapter(
    db: AsyncSession,
    project_id: UUID,
    chapter_id: UUID,
    user_id: UUID,
) -> ProjectChapter:
    chapter = await get_project_chapter_or_404(db, project_id, chapter_id, user_id)
    chapter.is_enabled = False
    chapter.updated_at = beijing_datetime()
    await db.commit()
    await db.refresh(chapter)
    return chapter
