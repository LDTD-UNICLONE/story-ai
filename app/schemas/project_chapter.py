from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import ConfigDict, Field, field_serializer

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


class ProjectChapterUpdateRequest(SchemaBaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=128)
    content: Optional[str] = Field(default=None, min_length=1)
    processed_content: Optional[str] = None
    sort_order: Optional[int] = Field(default=None, ge=0)
    processing_prompt: Optional[str] = Field(default=None, min_length=1)


class ProjectChapterProcessRequest(SchemaBaseModel):
    ai_model_id: UUID
    processing_prompt: Optional[str] = Field(default=None, min_length=1)
    extra: Optional[Dict[str, Any]] = None


class ProjectChapterProcessOut(SchemaBaseModel):
    chapter: ProjectChapterOut
    task_record_id: UUID
    points_cost: int
