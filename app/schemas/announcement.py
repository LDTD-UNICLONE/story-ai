from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import ConfigDict, Field
from app.schemas.base import SchemaBaseModel


class AnnouncementBaseOut(SchemaBaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    content: str
    image_url: Optional[str] = None
    link_url: Optional[str] = None
    announcement_type: str
    display_position: str
    style_config: Dict[str, Any]
    sort_order: int
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None


class AnnouncementOut(AnnouncementBaseOut):
    is_enabled: bool
    created_at: datetime
    updated_at: datetime


class AnnouncementListOut(SchemaBaseModel):
    items: List[AnnouncementOut]
    total: int
    page: int
    page_size: int


class AnnouncementCreateRequest(SchemaBaseModel):
    title: str = Field(..., min_length=1, max_length=128)
    content: str = Field(..., min_length=1)
    image_url: Optional[str] = Field(default=None, max_length=512)
    link_url: Optional[str] = Field(default=None, max_length=512)
    announcement_type: str = Field(default="notice", min_length=1, max_length=32)
    display_position: str = Field(default="home", min_length=1, max_length=32)
    style_config: Dict[str, Any] = Field(default_factory=dict)
    sort_order: int = 0
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None
    is_enabled: bool = True


class AnnouncementUpdateRequest(SchemaBaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=128)
    content: Optional[str] = Field(default=None, min_length=1)
    image_url: Optional[str] = Field(default=None, max_length=512)
    link_url: Optional[str] = Field(default=None, max_length=512)
    announcement_type: Optional[str] = Field(default=None, min_length=1, max_length=32)
    display_position: Optional[str] = Field(default=None, min_length=1, max_length=32)
    style_config: Optional[Dict[str, Any]] = None
    sort_order: Optional[int] = None
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None
    is_enabled: Optional[bool] = None


class AnnouncementPreviewOut(AnnouncementBaseOut):
    preview_style: Dict[str, Any]
