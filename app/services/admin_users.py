from typing import List, Optional, Tuple
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException
from app.core.config import settings
from app.core.security import hash_password
from app.models.user import User
from app.schemas.user import AdminUserCreateRequest, AdminUserUpdateRequest
from app.services.points import change_user_points


async def list_users(
    db: AsyncSession,
    keyword: Optional[str],
    is_enabled: Optional[bool],
    page: int,
    page_size: int,
) -> Tuple[List[User], int]:
    conditions = []
    if keyword:
        pattern = f"%{keyword}%"
        conditions.append(
            or_(
                User.account.ilike(pattern),
                User.nickname.ilike(pattern),
                User.phone.ilike(pattern),
                User.email.ilike(pattern),
            )
        )
    if is_enabled is not None:
        conditions.append(User.is_enabled == is_enabled)

    query = select(User)
    count_query = select(func.count()).select_from(User)
    if conditions:
        query = query.where(*conditions)
        count_query = count_query.where(*conditions)

    total_result = await db.execute(count_query)
    total = total_result.scalar_one()

    result = await db.execute(
        query.order_by(User.created_at.asc()).offset((page - 1) * page_size).limit(page_size)
    )
    return list(result.scalars().all()), total


async def get_user_or_404(db: AsyncSession, user_id: UUID) -> User:
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise AppException("用户不存在", code=40401, status_code=404)
    return user


async def create_user(db: AsyncSession, payload: AdminUserCreateRequest) -> User:
    unique_conditions = [User.account == payload.account]
    if payload.phone:
        unique_conditions.append(User.phone == payload.phone)
    if payload.email:
        unique_conditions.append(User.email == payload.email)

    exists = await db.execute(select(User).where(or_(*unique_conditions)))
    if exists.scalar_one_or_none() is not None:
        raise AppException("账号、手机号或邮箱已存在", code=40901, status_code=409)

    user = User(
        account=payload.account,
        password_hash=hash_password(payload.password),
        nickname=payload.nickname,
        avatar=payload.avatar or settings.default_user_avatar,
        phone=payload.phone,
        email=payload.email,
        is_admin=payload.is_admin,
        is_enabled=payload.is_enabled,
    )
    db.add(user)
    try:
        await db.flush()
        if payload.points_balance > 0:
            await change_user_points(
                db,
                user_id=user.id,
                amount=payload.points_balance,
                transaction_type="admin_create",
                remark="管理员创建用户初始积分",
                auto_commit=False,
            )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise AppException("账号、手机号或邮箱已存在", code=40901, status_code=409) from exc

    await db.refresh(user)
    return user


async def update_user(db: AsyncSession, user_id: UUID, payload: AdminUserUpdateRequest) -> User:
    user = await get_user_or_404(db, user_id)
    update_data = payload.model_dump(exclude_unset=True)

    unique_conditions = []
    if "account" in update_data and update_data["account"] != user.account:
        unique_conditions.append(User.account == update_data["account"])
    if "phone" in update_data and update_data["phone"] and update_data["phone"] != user.phone:
        unique_conditions.append(User.phone == update_data["phone"])
    if "email" in update_data and update_data["email"] and update_data["email"] != user.email:
        unique_conditions.append(User.email == update_data["email"])

    if unique_conditions:
        exists = await db.execute(select(User).where(or_(*unique_conditions), User.id != user_id))
        if exists.scalar_one_or_none() is not None:
            raise AppException("账号、手机号或邮箱已存在", code=40901, status_code=409)

    for field, value in update_data.items():
        setattr(user, field, value)

    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise AppException("账号、手机号或邮箱已存在", code=40901, status_code=409) from exc

    await db.refresh(user)
    return user


async def reset_user_password(db: AsyncSession, user_id: UUID, password: str) -> User:
    user = await get_user_or_404(db, user_id)
    user.password_hash = hash_password(password)
    await db.commit()
    await db.refresh(user)
    return user


async def delete_user(db: AsyncSession, user_id: UUID, current_admin_id: UUID) -> None:
    if user_id == current_admin_id:
        raise AppException("不能删除当前登录管理员", code=40001, status_code=400)

    user = await get_user_or_404(db, user_id)
    user.is_enabled = False
    await db.commit()
