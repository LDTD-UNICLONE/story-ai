from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_optional_current_user
from app.core.responses import success
from app.db.session import get_db
from app.models.user import User
from app.schemas.work import WorkCreateRequest, WorkListOut, WorkUpdateRequest
from app.services.work_streaming import ( stream_work_media, stream_work_media_thumbnail,
    stream_work_upload_preview,
)
from app.services.works import (
    create_work,
    delete_work,
    get_upload_for_preview,
    get_work_detail,
    get_work_media_for_stream,
    like_work,
    list_my_works,
    list_public_works,
    unlike_work,
    update_work,
    upload_work_file,
)

router = APIRouter(prefix="/works")


@router.post("/uploads")
async def upload_my_work_file(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await upload_work_file(db, current_user, file)
    return success(data=result.model_dump(mode="json"), message="上传成功")


@router.get("/uploads/{upload_id}/preview")
async def preview_my_work_upload(
    upload_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    upload = await get_upload_for_preview(db, upload_id, current_user)
    await db.close()
    return await stream_work_upload_preview(upload)


@router.get("")
async def public_works(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_current_user),
):
    works, total = await list_public_works(db, current_user, page, page_size)
    data = WorkListOut(items=works, total=total, page=page, page_size=page_size)
    return success(data=data.model_dump(mode="json"))


@router.get("/mine")
async def my_works(
    visibility: Optional[str] = Query(default=None, pattern="^(public|private)$"),
    status: Optional[str] = Query(default=None, pattern="^(draft|published|hidden|deleted)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    works, total = await list_my_works(db, current_user, visibility, status, page, page_size)
    data = WorkListOut(items=works, total=total, page=page, page_size=page_size)
    return success(data=data.model_dump(mode="json"))


@router.post("")
async def create_my_work(
    payload: WorkCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    work = await create_work(db, current_user, payload)
    message = "保存草稿成功" if work.status == "draft" else "发布成功"
    return success(data=work.model_dump(mode="json"), message=message)


@router.get("/{work_id}")
async def work_detail(
    work_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_current_user),
):
    work = await get_work_detail(db, work_id, current_user)
    return success(data=work.model_dump(mode="json"))


@router.patch("/{work_id}")
async def update_my_work(
    work_id: UUID,
    payload: WorkUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    work = await update_work(db, work_id, current_user, payload)
    return success(data=work.model_dump(mode="json"), message="更新成功")


@router.delete("/{work_id}")
async def delete_my_work(
    work_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    work = await delete_work(db, work_id, current_user)
    return success(data=work.model_dump(mode="json"), message="删除成功")


@router.post("/{work_id}/like")
async def like_my_work(
    work_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    work = await like_work(db, work_id, current_user)
    return success(data=work.model_dump(mode="json"), message="点赞成功")


@router.delete("/{work_id}/like")
async def unlike_my_work(
    work_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    work = await unlike_work(db, work_id, current_user)
    return success(data=work.model_dump(mode="json"), message="取消点赞成功")


@router.get("/{work_id}/media/{media_id}/stream")
async def work_media_stream(
    work_id: UUID,
    media_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_current_user),
):
    media = await get_work_media_for_stream(db, work_id, media_id, current_user)
    await db.close()
    return await stream_work_media(media)


@router.get("/{work_id}/media/{media_id}/thumbnail")
async def work_media_thumbnail(
    work_id: UUID,
    media_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_current_user),
):
    media = await get_work_media_for_stream(db, work_id, media_id, current_user)
    await db.close()
    return await stream_work_media_thumbnail(media)
