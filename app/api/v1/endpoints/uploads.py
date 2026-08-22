from fastapi import APIRouter, Depends, File, Form, UploadFile

from app.api.deps import get_current_user
from app.core.responses import success
from app.models.user import User
from app.services.uploads import upload_story_file

router = APIRouter(prefix="/uploads")


@router.post("/file")
async def upload_file(
    file: UploadFile = File(...),
    category: str = Form(default=""),
    current_user: User = Depends(get_current_user),
):
    result = await upload_story_file(
        file,
        category=f"user-uploads/{current_user.id}/{category}",
    )
    return success(data=result.model_dump(mode="json"), message="上传成功")
