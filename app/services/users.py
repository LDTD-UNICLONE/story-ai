from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException
from app.models.user import User
from app.schemas.user import UserProfileUpdateRequest


async def update_current_user_profile(
    db: AsyncSession,
    *,
    user_id: UUID,
    payload: UserProfileUpdateRequest,
) -> User:
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise AppException("用户不存在", code=40401, status_code=404)

    update_data = payload.model_dump(exclude_unset=True, include={"nickname", "avatar"})
    for field, value in update_data.items():
        setattr(user, field, value)

    await db.commit()

    await db.refresh(user)
    return user
