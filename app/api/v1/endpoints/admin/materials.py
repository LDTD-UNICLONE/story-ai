import json
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin_user
from app.core.exceptions import AppException
from app.core.responses import success
from app.db.session import get_db
from app.models.user import User
from app.schemas.material import AdminMaterialListOut, MaterialUpdateRequest
from app.services.material_streaming import stream_material_image
from app.services.materials import (
    build_admin_material_out,
    create_material_from_upload,
    delete_material,
    get_material_or_404,
    list_materials,
    update_material,
)

router = APIRouter(prefix="/admin/materials")


@router.get("")
async def admin_list_materials(
    keyword: Optional[str] = Query(default=None, max_length=255),
    category: Optional[str] = Query(default=None, max_length=64),
    is_enabled: Optional[bool] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    materials, total = await list_materials(
        db,
        keyword=keyword,
        category=category,
        is_enabled=is_enabled,
        page=page,
        page_size=page_size,
    )
    data = AdminMaterialListOut(
        items=[build_admin_material_out(item) for item in materials],
        total=total,
        page=page,
        page_size=page_size,
    )
    return success(data=data.model_dump(mode="json"))


@router.post("")
async def admin_create_material(
    file: UploadFile = File(...),
    name: str = Form(...),
    category: str = Form(...),
    description: Optional[str] = Form(default=None),
    tags: Optional[str] = Form(default=None),
    sort_order: int = Form(default=0, ge=0),
    is_enabled: bool = Form(default=True),
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    material = await create_material_from_upload(
        db,
        file,
        name=name,
        category=category,
        description=description,
        tags=_parse_tags(tags),
        sort_order=sort_order,
        is_enabled=is_enabled,
    )
    return success(
        data=build_admin_material_out(material).model_dump(mode="json"),
        message="创建成功",
    )


@router.get("/{material_id}")
async def admin_get_material(
    material_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    material = await get_material_or_404(db, material_id)
    return success(data=build_admin_material_out(material).model_dump(mode="json"))


@router.patch("/{material_id}")
async def admin_update_material(
    material_id: UUID,
    file: Optional[UploadFile] = File(default=None),
    name: Optional[str] = Form(default=None),
    category: Optional[str] = Form(default=None),
    description: Optional[str] = Form(default=None),
    tags: Optional[str] = Form(default=None),
    sort_order: Optional[int] = Form(default=None, ge=0),
    is_enabled: Optional[bool] = Form(default=None),
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    payload = MaterialUpdateRequest(
        **{
            key: value
            for key, value in {
                "name": name,
                "category": category,
                "description": description,
                "tags": _parse_tags(tags) if tags is not None else None,
                "sort_order": sort_order,
                "is_enabled": is_enabled,
            }.items()
            if value is not None
        }
    )
    material = await update_material(db, material_id, payload, file=file)
    return success(
        data=build_admin_material_out(material).model_dump(mode="json"),
        message="更新成功",
    )


@router.delete("/{material_id}")
async def admin_delete_material(
    material_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    await delete_material(db, material_id)
    return success(message="删除成功")


@router.get("/{material_id}/image")
async def admin_material_image(
    material_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    material = await get_material_or_404(db, material_id)
    await db.close()
    return await stream_material_image(material)


def _parse_tags(tags: Optional[str]) -> list[str]:
    if not tags:
        return []
    value = tags.strip()
    if not value:
        return []
    if value.startswith("["):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise AppException("标签格式错误", code=40022, status_code=400) from exc
        if not isinstance(parsed, list):
            raise AppException("标签格式错误", code=40022, status_code=400)
        return [str(item) for item in parsed]
    return [item.strip() for item in value.split(",") if item.strip()]
