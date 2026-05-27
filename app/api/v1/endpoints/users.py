from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.responses import success
from app.db.session import get_db
from app.models.user import User
from app.schemas.user import UserOut, UserProfileUpdateRequest
from app.services.users import update_current_user_profile

router = APIRouter(prefix="/users")


@router.patch("/me/profile")
async def update_my_profile(
    payload: UserProfileUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    user = await update_current_user_profile(db, user_id=current_user.id, payload=payload)
    return success(data=UserOut.model_validate(user).model_dump(mode="json"), message="更新成功")
