from typing import Iterable, List, Optional, Tuple
from uuid import UUID

from fastapi import UploadFile
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException
from app.models.material import Material
from app.schemas.material import (
    AdminMaterialOut,
    MaterialBaseOut,
    MaterialUpdateRequest,
)
from app.services.uploads import detect_content_type, upload_story_file

MATERIAL_UPLOAD_CATEGORY = "material"


def build_material_out(material: Material) -> MaterialBaseOut:
    return MaterialBaseOut(
        id=material.id,
        name=material.name,
        category=material.category,
        description=material.description,
        tags=material.tags or [],
        image_url=material.image_url,
        filename=material.filename,
        content_type=material.content_type,
        size=material.size,
        sort_order=material.sort_order,
    )


def build_admin_material_out(material: Material) -> AdminMaterialOut:
    base = build_material_out(material).model_dump()
    return AdminMaterialOut(
        **base,
        is_enabled=material.is_enabled,
        created_at=material.created_at,
        updated_at=material.updated_at,
        image_object_key=material.image_object_key,
    )


async def list_materials(
    db: AsyncSession,
    keyword: Optional[str],
    category: Optional[str],
    is_enabled: Optional[bool],
    page: int,
    page_size: int,
) -> Tuple[List[Material], int]:
    conditions = []
    if keyword:
        pattern = f"%{keyword}%"
        conditions.append(
            or_(
                Material.name.ilike(pattern),
                Material.category.ilike(pattern),
                Material.description.ilike(pattern),
            )
        )
    if category:
        conditions.append(Material.category == category)
    if is_enabled is not None:
        conditions.append(Material.is_enabled == is_enabled)

    query = select(Material)
    count_query = select(func.count()).select_from(Material)
    if conditions:
        query = query.where(*conditions)
        count_query = count_query.where(*conditions)

    total_result = await db.execute(count_query)
    total = total_result.scalar_one()
    result = await db.execute(
        query.order_by(Material.sort_order.asc(), Material.created_at.desc(), Material.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(result.scalars().all()), total


async def list_enabled_materials(
    db: AsyncSession,
    keyword: Optional[str],
    category: Optional[str],
    page: int,
    page_size: int,
) -> Tuple[List[Material], int]:
    return await list_materials(
        db,
        keyword=keyword,
        category=category,
        is_enabled=True,
        page=page,
        page_size=page_size,
    )


async def list_enabled_material_categories(db: AsyncSession) -> List[str]:
    result = await db.execute(
        select(Material.category)
        .where(Material.is_enabled.is_(True))
        .distinct()
        .order_by(Material.category.asc())
    )
    return list(result.scalars().all())


async def get_material_or_404(db: AsyncSession, material_id: UUID) -> Material:
    result = await db.execute(select(Material).where(Material.id == material_id))
    material = result.scalar_one_or_none()
    if material is None:
        raise AppException("素材不存在", code=40410, status_code=404)
    return material


async def get_enabled_material_or_404(db: AsyncSession, material_id: UUID) -> Material:
    result = await db.execute(
        select(Material).where(Material.id == material_id, Material.is_enabled.is_(True))
    )
    material = result.scalar_one_or_none()
    if material is None:
        raise AppException("素材不存在或未启用", code=40410, status_code=404)
    return material


async def create_material_from_upload(
    db: AsyncSession,
    file: UploadFile,
    *,
    name: str,
    category: str,
    description: Optional[str] = None,
    tags: Optional[Iterable[str]] = None,
    sort_order: int = 0,
    is_enabled: bool = True,
) -> Material:
    content_type = detect_content_type(file.filename or "file", file.content_type or "")
    if not content_type.startswith("image/"):
        raise AppException("素材库仅支持上传图像文件", code=40020, status_code=400)

    uploaded = await upload_story_file(
        file,
        category=MATERIAL_UPLOAD_CATEGORY,
        media_only=True,
    )
    material = Material(
        name=_clean_required_text(name, "图像名", 128),
        category=_clean_required_text(category, "分类", 64),
        description=_clean_optional_text(description, 2000),
        tags=_normalize_tags(tags),
        image_url=uploaded.url,
        image_object_key=uploaded.object_key,
        filename=uploaded.filename,
        content_type=uploaded.content_type,
        size=uploaded.size,
        sort_order=sort_order,
        is_enabled=is_enabled,
    )
    db.add(material)
    await db.commit()
    await db.refresh(material)
    return material


async def update_material(
    db: AsyncSession,
    material_id: UUID,
    payload: MaterialUpdateRequest,
    file: Optional[UploadFile] = None,
) -> Material:
    material = await get_material_or_404(db, material_id)
    update_data = payload.model_dump(exclude_unset=True)

    if "name" in update_data:
        material.name = _clean_required_text(update_data["name"], "图像名", 128)
    if "category" in update_data:
        material.category = _clean_required_text(update_data["category"], "分类", 64)
    if "description" in update_data:
        material.description = _clean_optional_text(update_data["description"], 2000)
    if "tags" in update_data:
        material.tags = _normalize_tags(update_data["tags"])
    if "sort_order" in update_data:
        material.sort_order = update_data["sort_order"]
    if "is_enabled" in update_data:
        material.is_enabled = update_data["is_enabled"]

    if file is not None and file.filename:
        content_type = detect_content_type(file.filename, file.content_type or "")
        if not content_type.startswith("image/"):
            raise AppException("素材库仅支持上传图像文件", code=40020, status_code=400)
        uploaded = await upload_story_file(
            file,
            category=MATERIAL_UPLOAD_CATEGORY,
            media_only=True,
        )
        material.image_url = uploaded.url
        material.image_object_key = uploaded.object_key
        material.filename = uploaded.filename
        material.content_type = uploaded.content_type
        material.size = uploaded.size

    await db.commit()
    await db.refresh(material)
    return material


async def delete_material(db: AsyncSession, material_id: UUID) -> None:
    material = await get_material_or_404(db, material_id)
    material.is_enabled = False
    await db.commit()


def _normalize_tags(tags: Optional[Iterable[str]]) -> List[str]:
    if not tags:
        return []
    normalized = []
    seen = set()
    for tag in tags:
        cleaned = str(tag).strip()
        if not cleaned or cleaned in seen:
            continue
        normalized.append(cleaned[:64])
        seen.add(cleaned)
    return normalized[:20]


def _clean_required_text(value: str, field_name: str, max_length: int) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        raise AppException(f"{field_name}不能为空", code=40021, status_code=400)
    return cleaned[:max_length]


def _clean_optional_text(value: Optional[str], max_length: int) -> Optional[str]:
    cleaned = (value or "").strip()
    return cleaned[:max_length] if cleaned else None
