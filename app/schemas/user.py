from typing import List, Optional
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator
from app.schemas.base import SchemaBaseModel


class UserOut(SchemaBaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    account: str
    nickname: str
    avatar: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    is_admin: bool
    is_enabled: bool
    points_balance: int


class UserListOut(SchemaBaseModel):
    items: List[UserOut]
    total: int
    page: int
    page_size: int


class AdminUserCreateRequest(SchemaBaseModel):
    account: str = Field(..., min_length=3, max_length=64)
    password: str = Field(..., min_length=6, max_length=128)
    nickname: str = Field(..., min_length=1, max_length=64)
    avatar: Optional[str] = Field(default=None, max_length=512)
    phone: Optional[str] = Field(default=None, max_length=32)
    email: Optional[str] = Field(default=None, max_length=255)
    is_admin: bool = False
    is_enabled: bool = True
    points_balance: int = Field(default=0, ge=0)


class AdminUserUpdateRequest(SchemaBaseModel):
    account: Optional[str] = Field(default=None, min_length=3, max_length=64)
    nickname: Optional[str] = Field(default=None, min_length=1, max_length=64)
    avatar: Optional[str] = Field(default=None, max_length=512)
    phone: Optional[str] = Field(default=None, max_length=32)
    email: Optional[str] = Field(default=None, max_length=255)
    is_admin: Optional[bool] = None
    is_enabled: Optional[bool] = None


class AdminPasswordResetRequest(SchemaBaseModel):
    password: str = Field(..., min_length=6, max_length=128)


class RegisterRequest(SchemaBaseModel):
    account: str = Field(..., min_length=3, max_length=64)
    password: str = Field(..., min_length=6, max_length=128)
    nickname: str = Field(..., min_length=1, max_length=64)
    avatar: Optional[str] = Field(default=None, max_length=512)
    phone: str = Field(..., min_length=11, max_length=11)
    sms_code: str = Field(..., min_length=4, max_length=8, description="手机短信验证码")
    email: Optional[str] = Field(default=None, max_length=255)

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        value = value.strip()
        if len(value) != 11 or not value.isdigit() or value[0] != "1":
            raise ValueError("手机号格式不正确")
        return value

    @field_validator("sms_code")
    @classmethod
    def validate_sms_code(cls, value: str) -> str:
        value = value.strip()
        if not value.isdigit():
            raise ValueError("验证码必须为数字")
        return value


class SendRegisterSmsCodeRequest(SchemaBaseModel):
    phone: str = Field(..., min_length=11, max_length=11)

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        value = value.strip()
        if len(value) != 11 or not value.isdigit() or value[0] != "1":
            raise ValueError("手机号格式不正确")
        return value


class LoginRequest(SchemaBaseModel):
    identifier: str = Field(..., min_length=1, max_length=255, description="账号、手机号或邮箱")
    password: str = Field(..., min_length=1, max_length=128)


class TokenOut(SchemaBaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut
