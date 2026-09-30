from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import ConfigDict, Field, field_serializer, field_validator

from app.core.public_messages import sanitize_public_data
from app.schemas.base import SchemaBaseModel


class ProjectChapterOut(SchemaBaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    user_id: UUID
    ai_model_id: Optional[UUID] = None
    title: str
    content: str
    processing_prompt: Optional[str] = None
    processed_content: Optional[str] = None
    process_status: str
    sort_order: int
    extra: Dict[str, Any]
    is_enabled: bool
    created_at: datetime
    updated_at: datetime

    @field_serializer("extra")
    def serialize_extra(self, value: Dict[str, Any]) -> Dict[str, Any]:
        return sanitize_public_data(value)


class ProjectChapterListOut(SchemaBaseModel):
    items: List[ProjectChapterOut]
    total: int
    page: int
    page_size: int


class ProjectChapterCreateRequest(SchemaBaseModel):
    title: str = Field(..., min_length=1, max_length=128)
    content: str = Field(..., min_length=1)
    sort_order: int = Field(default=0, ge=0)
    processing_prompt: Optional[str] = Field(default=None, min_length=1)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("章节标题不能为空")
        return value

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("章节内容不能为空")
        return value


class ProjectChapterUpdateRequest(SchemaBaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=128)
    content: Optional[str] = Field(default=None, min_length=1)
    processed_content: Optional[str] = None
    sort_order: Optional[int] = Field(default=None, ge=0)
    processing_prompt: Optional[str] = Field(default=None, min_length=1)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: Optional[str]) -> str:
        if value is None:
            raise ValueError("章节标题不能为 null")
        value = value.strip()
        if not value:
            raise ValueError("章节标题不能为空")
        return value

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: Optional[str]) -> str:
        if value is None:
            raise ValueError("章节内容不能为 null")
        if not value.strip():
            raise ValueError("章节内容不能为空")
        return value

    @field_validator("sort_order")
    @classmethod
    def reject_null_sort_order(cls, value: Optional[int]) -> int:
        if value is None:
            raise ValueError("章节排序不能为 null")
        return value
