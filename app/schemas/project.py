from datetime import datetime
from typing import List, Literal, Optional
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator
from app.schemas.base import SchemaBaseModel


ProjectGenerationRatio = Literal["21:9", "16:9", "4:3", "1:1", "3:4", "9:16"]


class ProjectOut(SchemaBaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    name: str
    cover: str
    description: str
    project_kind: Literal["standard", "agent"]
    is_enabled: bool
    created_at: datetime
    updated_at: datetime


class ProjectListOut(SchemaBaseModel):
    items: List[ProjectOut]
    total: int
    page: int
    page_size: int


class ProjectCreateRequest(SchemaBaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=128)
    cover: Optional[str] = Field(default="", max_length=512)
    description: Optional[str] = Field(default="")

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("项目名称不能为空")
        return value


class ProjectUpdateRequest(SchemaBaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    cover: Optional[str] = Field(default=None, max_length=512)
    description: Optional[str] = Field(default=None)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: Optional[str]) -> str:
        if value is None:
            raise ValueError("项目名称不能为 null")
        value = value.strip()
        if not value:
            raise ValueError("项目名称不能为空")
        return value

    @field_validator("cover", "description")
    @classmethod
    def reject_null_required_fields(cls, value):
        if value is None:
            raise ValueError("字段不能为 null")
        return value
