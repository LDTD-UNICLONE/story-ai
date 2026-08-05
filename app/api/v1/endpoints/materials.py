from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.responses import success
from app.db.session import get_db
from app.models.user import User
from app.schemas.material import MaterialListOut
from app.services.materials import (
    build_material_out,
    get_enabled_material_or_404,
    list_enabled_material_categories,
    list_enabled_materials,
)
from app.services.material_streaming import stream_material_image

router = APIRouter(prefix="/materials")


@router.get("")
async def material_options(
    keyword: Optional[str] = Query(default=None, max_length=255),
    category: Optional[str] = Query(default=None, max_length=64),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    materials, total = await list_enabled_materials(
        db,
        keyword=keyword,
        category=category,
        page=page,
        page_size=page_size,
    )
    data = MaterialListOut(
        items=[build_material_out(item) for item in materials],
        total=total,
        page=page,
        page_size=page_size,
    )
    return success(data=data.model_dump(mode="json"))


@router.get("/categories")
async def material_categories(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return success(data=await list_enabled_material_categories(db))


@router.get("/{material_id}")
async def material_detail(
    material_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    material = await get_enabled_material_or_404(db, material_id)
    return success(data=build_material_out(material).model_dump(mode="json"))


@router.get("/{material_id}/image")
async def material_image(
    material_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    material = await get_enabled_material_or_404(db, material_id)
    await db.close()
    return await stream_material_image(material)
