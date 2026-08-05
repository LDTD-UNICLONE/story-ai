from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from app.core.announcement_security import (
    safe_announcement_url,
    sanitize_announcement_content,
)
from app.schemas.base import SchemaBaseModel


class AnnouncementBaseOut(SchemaBaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    content: str
    content_format: str
    image_url: Optional[str] = None
    link_url: Optional[str] = None
    announcement_type: str
    display_position: str
    style_config: Dict[str, Any]
    sort_order: int
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None

    @model_validator(mode="after")
    def sanitize_public_content(self) -> "AnnouncementBaseOut":
        self.content = sanitize_announcement_content(
            self.content,
            self.content_format,
            strict=False,
        )
        self.image_url = safe_announcement_url(self.image_url)
        self.link_url = safe_announcement_url(self.link_url)
        return self


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
    content_format: Literal["plain", "markdown", "html"] = "plain"
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
    content_format: Optional[Literal["plain", "markdown", "html"]] = None
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
