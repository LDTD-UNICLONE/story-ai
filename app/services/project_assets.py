from typing import Any, Dict, List, Optional, Tuple, Type, Union
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException
from app.core.timezone import beijing_datetime
from app.models.project_asset import ProjectCharacter, ProjectProp, ProjectScene
from app.models.project_chapter import ProjectChapter
from app.services.projects import get_project_or_404


AssetModel = Union[Type[ProjectCharacter], Type[ProjectScene], Type[ProjectProp]]


async def list_project_assets(
    db: AsyncSession,
    model: AssetModel,
    project_id: UUID,
    user_id: UUID,
    keyword: Optional[str],
    page: int,
    page_size: int,
) -> Tuple[List[Any], int]:
    await get_project_or_404(db, project_id, user_id)
    conditions = [model.project_id == project_id, model.user_id == user_id, model.is_enabled.is_(True)]
    if keyword:
        pattern = f"%{keyword}%"
        conditions.append(or_(model.name.ilike(pattern), model.description.ilike(pattern), model.prompt.ilike(pattern)))

    count_result = await db.execute(select(func.count()).select_from(model).where(*conditions))
    total = count_result.scalar_one()

    result = await db.execute(
        select(model)
        .where(*conditions)
        .order_by(model.created_at.desc(), model.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(result.scalars().all()), total


async def list_project_asset_options(
    db: AsyncSession,
    model: AssetModel,
    asset_type: str,
    project_id: UUID,
    user_id: UUID,
    keyword: Optional[str],
    limit: int,
) -> Tuple[List[Dict[str, Any]], int]:
    await get_project_or_404(db, project_id, user_id)
    conditions = [model.project_id == project_id, model.user_id == user_id, model.is_enabled.is_(True)]
    if keyword:
        pattern = f"%{keyword}%"
        conditions.append(or_(model.name.ilike(pattern), model.description.ilike(pattern), model.prompt.ilike(pattern)))

    count_result = await db.execute(select(func.count()).select_from(model).where(*conditions))
    total = count_result.scalar_one()
    result = await db.execute(
        select(model)
        .where(*conditions)
        .order_by(model.created_at.desc(), model.id.desc())
        .limit(limit)
    )
    return [_asset_option_payload(item, asset_type) for item in result.scalars().all()], total


def _asset_option_payload(asset: Any, asset_type: str) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "id": asset.id,
        "asset_type": asset_type,
        "name": asset.name,
        "reference_image": asset.reference_image,
        "source_chapter_id": asset.source_chapter_id,
    }
    if isinstance(asset, ProjectCharacter):
        data.update({"aliases": asset.aliases or [], "identity": asset.identity})
    elif isinstance(asset, ProjectScene):
        data.update({"location": asset.location, "time_of_day": asset.time_of_day})
    elif isinstance(asset, ProjectProp):
        data.update({"category": asset.category})
    return data


async def get_project_asset_or_404(
    db: AsyncSession,
    model: AssetModel,
    project_id: UUID,
    asset_id: UUID,
    user_id: UUID,
) -> Any:
    result = await db.execute(
        select(model).where(
            model.id == asset_id,
            model.project_id == project_id,
            model.user_id == user_id,
            model.is_enabled.is_(True),
        )
    )
    asset = result.scalar_one_or_none()
    if asset is None:
        raise AppException("项目资源不存在", code=40409, status_code=404)
    return asset


async def create_project_asset(
    db: AsyncSession,
    model: AssetModel,
    project_id: UUID,
    user_id: UUID,
    payload: Any,
) -> Any:
    await get_project_or_404(db, project_id, user_id)
    data = payload.model_dump()
    await _validate_source_chapter(db, project_id, user_id, data.get("source_chapter_id"))
    asset = model(project_id=project_id, user_id=user_id, **data)
    db.add(asset)
    await db.commit()
    await db.refresh(asset)
    return asset


async def update_project_asset(
    db: AsyncSession,
    model: AssetModel,
    project_id: UUID,
    asset_id: UUID,
    user_id: UUID,
    payload: Any,
) -> Any:
    asset = await get_project_asset_or_404(db, model, project_id, asset_id, user_id)
    data: Dict[str, Any] = payload.model_dump(exclude_unset=True)
    await _validate_source_chapter(db, project_id, user_id, data.get("source_chapter_id"))
    for field, value in data.items():
        setattr(asset, field, value)
    asset.updated_at = beijing_datetime()
    await db.commit()
    await db.refresh(asset)
    return asset


async def delete_project_asset(
    db: AsyncSession,
    model: AssetModel,
    project_id: UUID,
    asset_id: UUID,
    user_id: UUID,
) -> Any:
    asset = await get_project_asset_or_404(db, model, project_id, asset_id, user_id)
    asset.is_enabled = False
    asset.updated_at = beijing_datetime()
    await db.commit()
    await db.refresh(asset)
    return asset


async def _validate_source_chapter(
    db: AsyncSession,
    project_id: UUID,
    user_id: UUID,
    source_chapter_id: Optional[UUID],
) -> None:
    if source_chapter_id is None:
        return
    result = await db.execute(
        select(ProjectChapter.id).where(
            ProjectChapter.id == source_chapter_id,
            ProjectChapter.project_id == project_id,
            ProjectChapter.user_id == user_id,
            ProjectChapter.is_enabled.is_(True),
        )
    )
    if result.scalar_one_or_none() is None:
        raise AppException("来源章节不存在", code=40408, status_code=404)
