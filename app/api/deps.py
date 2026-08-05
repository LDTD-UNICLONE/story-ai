from typing import Optional
from uuid import UUID

import jwt
from fastapi import Depends, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException
from app.core.logging import bind_request_context
from app.core.security import decode_access_token
from app.db.session import get_db
from app.models.user import User
from app.services.auth import get_user_by_id

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


async def get_current_user(
    request: Request,
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    return await _authenticate_access_token(request, token, db)


async def get_optional_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Optional[User]:
    authorization = request.headers.get("Authorization")
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AppException("无效的登录凭证", code=40102, status_code=401)
    return await _authenticate_access_token(request, token.strip(), db)


async def _authenticate_access_token(
    request: Request,
    token: str,
    db: AsyncSession,
) -> User:
    try:
        payload = decode_access_token(token)
        subject = payload.get("sub")
        if subject is None:
            raise AppException("无效的登录凭证", code=40102, status_code=401)
        user_id = UUID(subject)
        token_version = int(payload.get("ver", 0))
    except (jwt.PyJWTError, TypeError, ValueError) as exc:
        raise AppException("无效的登录凭证", code=40102, status_code=401) from exc

    user = await get_user_by_id(db, user_id)
    if user is None:
        raise AppException("用户不存在或已被删除", code=40103, status_code=401)
    if not user.is_enabled:
        raise AppException("账号已被禁用", code=40302, status_code=403)
    if token_version != user.token_version:
        raise AppException("登录凭证已失效，请重新登录", code=40102, status_code=401)
    bind_request_context(user_id=str(user.id))
    request.state.user_id = str(user.id)
    return user


async def get_current_admin_user(current_user: User = Depends(get_current_user)) -> User:
    if not current_user.is_admin:
        raise AppException("无管理员权限", code=40301, status_code=403)
    return current_user
