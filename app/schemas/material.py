from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import ConfigDict, Field

from app.schemas.base import SchemaBaseModel


class MaterialBaseOut(SchemaBaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    category: str
    description: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    image_url: str
    filename: str
    content_type: str
    size: int
    sort_order: int


class MaterialOut(MaterialBaseOut):
    is_enabled: bool
    created_at: datetime
    updated_at: datetime


class AdminMaterialOut(MaterialOut):
    image_object_key: str


class MaterialListOut(SchemaBaseModel):
    items: List[MaterialBaseOut]
    total: int
    page: int
    page_size: int


class AdminMaterialListOut(SchemaBaseModel):
    items: List[AdminMaterialOut]
    total: int
    page: int
    page_size: int


class MaterialUpdateRequest(SchemaBaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    category: Optional[str] = Field(default=None, min_length=1, max_length=64)
    description: Optional[str] = Field(default=None, max_length=2000)
    tags: Optional[List[str]] = None
    sort_order: Optional[int] = Field(default=None, ge=0)
    is_enabled: Optional[bool] = None
