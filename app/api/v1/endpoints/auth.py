from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.responses import success
from app.db.session import get_db
from app.models.user import User
from app.schemas.user import LoginRequest, RegisterRequest, SendRegisterSmsCodeRequest, UserOut
from app.services.auth import login_user, register_user, send_register_code

router = APIRouter(prefix="/auth")


@router.post("/register/sms-code")
async def send_register_sms(payload: SendRegisterSmsCodeRequest, db: AsyncSession = Depends(get_db)):
    await send_register_code(db, payload)
    return success(message="验证码已发送")


@router.post("/register")
async def register(payload: RegisterRequest, db: AsyncSession = Depends(get_db)):
    user = await register_user(db, payload)
    return success(data=UserOut.model_validate(user).model_dump(mode="json"), message="注册成功")


@router.post("/login")
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)):
    token = await login_user(db, payload)
    return success(data=token.model_dump(mode="json"), message="登录成功")


@router.get("/me")
async def me(current_user: User = Depends(get_current_user)):
    return success(data=UserOut.model_validate(current_user).model_dump(mode="json"))
