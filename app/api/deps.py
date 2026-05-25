from uuid import UUID

import jwt
from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException
from app.core.security import decode_access_token
from app.db.session import get_db
from app.models.user import User
from app.services.auth import get_user_by_id

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    try:
        payload = decode_access_token(token)
        subject = payload.get("sub")
        if subject is None:
            raise AppException("无效的登录凭证", code=40102, status_code=401)
        user_id = UUID(subject)
    except (jwt.PyJWTError, ValueError) as exc:
        raise AppException("无效的登录凭证", code=40102, status_code=401) from exc

    user = await get_user_by_id(db, user_id)
    if user is None:
        raise AppException("用户不存在或已被删除", code=40103, status_code=401)
    if not user.is_enabled:
        raise AppException("账号已被禁用", code=40302, status_code=403)
    return user


async def get_current_admin_user(current_user: User = Depends(get_current_user)) -> User:
    if not current_user.is_admin:
        raise AppException("无管理员权限", code=40301, status_code=403)
    return current_user
