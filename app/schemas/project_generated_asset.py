from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import ConfigDict, Field, field_serializer

from app.core.public_messages import sanitize_public_data
from app.schemas.base import SchemaBaseModel


class ProjectGeneratedAssetOut(SchemaBaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    chapter_id: Optional[UUID] = None
    user_id: UUID
    task_record_id: Optional[UUID] = None
    ai_model_id: Optional[UUID] = None
    target_type: str
    target_id: UUID
    media_type: str
    result_url: Optional[str] = None
    result_urls: List[str]
    last_frame_url: Optional[str] = None
    prompt: Optional[str] = None
    generation_mode: Optional[str] = None
    status: str
    is_selected: bool
    extra: Dict[str, Any]
    is_enabled: bool
    created_at: datetime
    updated_at: datetime

    @field_serializer("extra")
    def serialize_extra(self, value: Dict[str, Any]) -> Dict[str, Any]:
        return sanitize_public_data(value)


class ProjectGeneratedAssetListOut(SchemaBaseModel):
    items: List[ProjectGeneratedAssetOut]
    total: int
    page: int
    page_size: int


class ProjectGeneratedAssetSelectRequest(SchemaBaseModel):
    result_url: Optional[str] = Field(default=None, max_length=1024)


class ProjectGeneratedAssetSelectOut(SchemaBaseModel):
    history: ProjectGeneratedAssetOut
    selected_url: Optional[str] = None
    last_frame_url: Optional[str] = None
