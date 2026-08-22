from datetime import datetime
from typing import List, Literal, Optional
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator
from app.schemas.base import SchemaBaseModel

from app.schemas.style import StyleBaseOut


ProjectGenerationRatio = Literal["21:9", "16:9", "4:3", "1:1", "3:4", "9:16"]


class ProjectOut(SchemaBaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    style_id: Optional[UUID] = None
    name: str
    cover: str
    description: str
    generation_ratio: Optional[str] = None
    project_kind: Literal["standard", "agent"]
    is_enabled: bool
    style: Optional[StyleBaseOut] = None
    created_at: datetime
    updated_at: datetime


class ProjectListOut(SchemaBaseModel):
    items: List[ProjectOut]
    total: int
    page: int
    page_size: int


class ProjectCreateRequest(SchemaBaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    cover: Optional[str] = Field(default="", max_length=512)
    description: Optional[str] = Field(default="")
    generation_ratio: ProjectGenerationRatio
    style_id: UUID

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("项目名称不能为空")
        return value


class ProjectUpdateRequest(SchemaBaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    cover: Optional[str] = Field(default=None, max_length=512)
    description: Optional[str] = Field(default=None)
    generation_ratio: Optional[ProjectGenerationRatio] = None
    style_id: Optional[UUID] = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: Optional[str]) -> str:
        if value is None:
            raise ValueError("项目名称不能为 null")
        value = value.strip()
        if not value:
            raise ValueError("项目名称不能为空")
        return value

    @field_validator("cover", "description", "generation_ratio", "style_id")
    @classmethod
    def reject_null_required_fields(cls, value):
        if value is None:
            raise ValueError("字段不能为 null")
        return value
