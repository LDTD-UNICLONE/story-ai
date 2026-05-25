from typing import Optional
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import AppException
from app.core.security import create_access_token, hash_password, verify_password
from app.models.user import User
from app.schemas.user import LoginRequest, RegisterRequest, SendRegisterSmsCodeRequest, TokenOut, UserOut
from app.services.phone_verification import (
    assert_register_sms_code,
    consume_register_sms_code,
    send_register_sms_code,
)


async def get_user_by_id(db: AsyncSession, user_id: UUID) -> Optional[User]:
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def get_user_by_identity(db: AsyncSession, identity: str) -> Optional[User]:
    result = await db.execute(
        select(User).where(or_(User.account == identity, User.phone == identity, User.email == identity))
    )
    return result.scalar_one_or_none()


async def register_user(db: AsyncSession, payload: RegisterRequest) -> User:
    await assert_register_sms_code(payload.phone, payload.sms_code)

    unique_conditions = [User.account == payload.account]
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
        is_admin=False,
        is_enabled=True,
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise AppException("账号、手机号或邮箱已存在", code=40901, status_code=409) from exc

    await db.refresh(user)
    await consume_register_sms_code(payload.phone)
    return user


async def send_register_code(db: AsyncSession, payload: SendRegisterSmsCodeRequest) -> None:
    exists = await db.execute(select(User).where(User.phone == payload.phone))
    if exists.scalar_one_or_none() is not None:
        raise AppException("手机号已存在", code=40902, status_code=409)
    await send_register_sms_code(payload.phone)


async def login_user(db: AsyncSession, payload: LoginRequest) -> TokenOut:
    user = await get_user_by_identity(db, payload.identifier)
    if user is None or not verify_password(payload.password, user.password_hash):
        raise AppException("账号或密码错误", code=40101, status_code=401)
    if not user.is_enabled:
        raise AppException("账号已被禁用", code=40302, status_code=403)

    token = create_access_token(str(user.id))
    return TokenOut(
        access_token=token,
        expires_in=settings.access_token_expire_minutes * 60,
        user=UserOut.model_validate(user),
    )
