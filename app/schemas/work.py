from datetime import datetime
from typing import List, Literal, Optional
from uuid import UUID

from pydantic import Field, model_validator

from app.schemas.base import SchemaBaseModel


WorkVisibility = Literal["public", "private"]
WorkStatus = Literal["draft", "published", "hidden", "deleted"]
WorkOwnerStatus = Literal["draft", "published"]
WorkMediaType = Literal["image", "video"]


class WorkUploadOut(SchemaBaseModel):
    upload_id: UUID
    media_type: str
    url: str
    filename: str
    content_type: str
    size: int
    preview_url: str


class WorkMediaCreateItem(SchemaBaseModel):
    upload_id: UUID
    sort_order: int = Field(default=0, ge=0)


class WorkMediaUpdateItem(SchemaBaseModel):
    media_id: Optional[UUID] = None
    upload_id: Optional[UUID] = None
    sort_order: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_media_source(self) -> "WorkMediaUpdateItem":
        if bool(self.media_id) == bool(self.upload_id):
            raise ValueError("作品媒体项必须且只能包含 media_id 或 upload_id")
        return self


class WorkCreateRequest(SchemaBaseModel):
    title: str = Field(..., min_length=1, max_length=128)
    description: Optional[str] = Field(default=None, max_length=2000)
    visibility: WorkVisibility = "public"
    status: WorkOwnerStatus = "published"
    media_items: List[WorkMediaCreateItem] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_published_media(self) -> "WorkCreateRequest":
        if self.status == "published" and not self.media_items:
            raise ValueError("发布作品至少需要一个媒体文件")
        return self


class WorkUpdateRequest(SchemaBaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=128)
    description: Optional[str] = Field(default=None, max_length=2000)
    visibility: Optional[WorkVisibility] = None
    status: Optional[WorkOwnerStatus] = None
    media_items: Optional[List[WorkMediaUpdateItem]] = Field(default=None, max_length=20)


class AdminWorkUpdateRequest(SchemaBaseModel):
    visibility: Optional[WorkVisibility] = None
    status: Optional[WorkStatus] = None


class WorkMediaOut(SchemaBaseModel):
    id: UUID
    media_type: str
    url: str
    filename: str
    content_type: str
    size: int
    width: Optional[int] = None
    height: Optional[int] = None
    duration_seconds: Optional[int] = None
    sort_order: int
    stream_url: str
    thumbnail_url: Optional[str] = None
    created_at: datetime


class WorkOut(SchemaBaseModel):
    id: UUID
    user_id: UUID
    title: str
    description: Optional[str] = None
    visibility: str
    status: str
    like_count: int
    view_count: int
    liked_by_me: bool = False
    media_count: int
    media_items: List[WorkMediaOut]
    created_at: datetime
    updated_at: datetime


class WorkListOut(SchemaBaseModel):
    items: List[WorkOut]
    total: int
    page: int
    page_size: int


class AdminWorkListOut(WorkListOut):
    pass
