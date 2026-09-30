from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.responses import success
from app.models.user import User
from app.schemas.upload import SeedanceImageRetryRequest
from app.db.session import get_db
from app.services.seedance_images import upload_seedance_image, get_image, image_out, retry_image
from app.services.uploads import upload_story_file

router = APIRouter(prefix="/uploads")


@router.post("/file")
async def upload_file(
    file: UploadFile = File(...),
    category: str = Form(default=""),
    current_user: User = Depends(get_current_user),
    purpose: Annotated[Literal["", "seedance_reference"], Form()] = "",
    db: AsyncSession = Depends(get_db),
):
    if purpose == "seedance_reference":
        result = await upload_seedance_image(db, current_user.id, file)
        return success(data=result.model_dump(mode="json"), message="图片已接收")
    result = await upload_story_file(
        file,
        category=f"user-uploads/{current_user.id}/{category}",
    )
    return success(data=result.model_dump(mode="json"), message="上传成功")


@router.get("/images/{image_id}")
async def get_uploaded_image(
    image_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return success(data=image_out(await get_image(db, current_user.id, image_id)).model_dump(mode="json"))


@router.post("/images/{image_id}/retry-review")
async def retry_uploaded_image_review(
    image_id: UUID,
    payload: SeedanceImageRetryRequest | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await retry_image(
        db, current_user.id, image_id,
        confirm_resubmit=payload.confirm_resubmit if payload else False,
    )
    return success(data=result.model_dump(mode="json"))
