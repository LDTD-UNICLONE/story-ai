from typing import List, Optional, Tuple
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException
from app.models.style import Style
from app.schemas.style import StyleCreateRequest, StyleUpdateRequest


async def list_styles(
    db: AsyncSession,
    keyword: Optional[str],
    is_enabled: Optional[bool],
    page: int,
    page_size: int,
) -> Tuple[List[Style], int]:
    conditions = []
    if keyword:
        pattern = f"%{keyword}%"
        conditions.append(or_(Style.name.ilike(pattern), Style.prompt.ilike(pattern)))
    if is_enabled is not None:
        conditions.append(Style.is_enabled == is_enabled)

    query = select(Style)
    count_query = select(func.count()).select_from(Style)
    if conditions:
        query = query.where(*conditions)
        count_query = count_query.where(*conditions)

    total_result = await db.execute(count_query)
    total = total_result.scalar_one()

    result = await db.execute(
        query.order_by(Style.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    )
    return list(result.scalars().all()), total


async def list_enabled_styles(db: AsyncSession) -> List[Style]:
    result = await db.execute(
        select(Style).where(Style.is_enabled.is_(True)).order_by(Style.created_at.desc())
    )
    return list(result.scalars().all())


async def get_style_or_404(db: AsyncSession, style_id: UUID) -> Style:
    result = await db.execute(select(Style).where(Style.id == style_id))
    style = result.scalar_one_or_none()
    if style is None:
        raise AppException("风格不存在", code=40403, status_code=404)
    return style


async def get_enabled_style_or_404(db: AsyncSession, style_id: UUID) -> Style:
    result = await db.execute(select(Style).where(Style.id == style_id, Style.is_enabled.is_(True)))
    style = result.scalar_one_or_none()
    if style is None:
        raise AppException("风格不存在或未启用", code=40403, status_code=404)
    return style


async def create_style(db: AsyncSession, payload: StyleCreateRequest) -> Style:
    style = Style(**payload.model_dump())
    db.add(style)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise AppException("风格名称已存在", code=40903, status_code=409) from exc

    await db.refresh(style)
    return style


async def update_style(db: AsyncSession, style_id: UUID, payload: StyleUpdateRequest) -> Style:
    style = await get_style_or_404(db, style_id)
    update_data = payload.model_dump(exclude_unset=True)

    if "name" in update_data and update_data["name"] != style.name:
        exists = await db.execute(
            select(Style).where(Style.name == update_data["name"], Style.id != style_id)
        )
        if exists.scalar_one_or_none() is not None:
            raise AppException("风格名称已存在", code=40903, status_code=409)

    for field, value in update_data.items():
        setattr(style, field, value)

    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise AppException("风格名称已存在", code=40903, status_code=409) from exc

    await db.refresh(style)
    return style


async def delete_style(db: AsyncSession, style_id: UUID) -> None:
    style = await get_style_or_404(db, style_id)
    style.is_enabled = False
    await db.commit()
