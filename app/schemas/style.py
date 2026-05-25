from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import ConfigDict, Field
from app.schemas.base import SchemaBaseModel


class StyleBaseOut(SchemaBaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    cover: str
    version: str


class StyleOut(StyleBaseOut):
    prompt: str
    is_enabled: bool
    created_at: datetime
    updated_at: datetime


class StyleListOut(SchemaBaseModel):
    items: List[StyleOut]
    total: int
    page: int
    page_size: int


class StyleCreateRequest(SchemaBaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    cover: str = Field(..., min_length=1, max_length=512)
    prompt: str = Field(..., min_length=1)
    version: str = Field(default="v1", min_length=1, max_length=32)
    is_enabled: bool = True


class StyleUpdateRequest(SchemaBaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    cover: Optional[str] = Field(default=None, min_length=1, max_length=512)
    prompt: Optional[str] = Field(default=None, min_length=1)
    version: Optional[str] = Field(default=None, min_length=1, max_length=32)
    is_enabled: Optional[bool] = None
