from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import ConfigDict, Field
from app.schemas.base import SchemaBaseModel


class AiModelOut(SchemaBaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    nickname: str
    model_id: str
    vendor: str
    model_type: str
    remark: Optional[str] = None
    points_cost: int
    model_multiplier: Decimal
    cache_multiplier: Decimal
    completion_multiplier: Decimal
    platform_multiplier: Decimal
    is_enabled: bool
    capabilities: Dict[str, Any]
    created_at: datetime
    updated_at: datetime


class AiModelListOut(SchemaBaseModel):
    items: List[AiModelOut]
    total: int
    page: int
    page_size: int


class AiModelOptionOut(SchemaBaseModel):
    id: UUID
    nickname: str
    model_id: str
    vendor: str
    model_type: str
    remark: Optional[str] = None
    points_cost: int
    model_multiplier: Decimal
    cache_multiplier: Decimal
    completion_multiplier: Decimal
    platform_multiplier: Decimal
    capabilities: Dict[str, Any]

    model_config = ConfigDict(from_attributes=True)


class AiModelCreateRequest(SchemaBaseModel):
    nickname: str = Field(..., min_length=1, max_length=64)
    model_id: str = Field(..., min_length=1, max_length=128)
    vendor: str = Field(..., min_length=1, max_length=64)
    model_type: str = Field(..., min_length=1, max_length=64)
    remark: Optional[str] = None
    points_cost: int = Field(default=0, ge=0)
    model_multiplier: Decimal = Field(default=Decimal("1.0000"), ge=0)
    cache_multiplier: Decimal = Field(default=Decimal("1.0000"), ge=0)
    completion_multiplier: Decimal = Field(default=Decimal("1.0000"), ge=0)
    platform_multiplier: Decimal = Field(default=Decimal("1.0000"), ge=0)
    is_enabled: bool = True
    capabilities: Dict[str, Any] = Field(default_factory=dict)


class AiModelUpdateRequest(SchemaBaseModel):
    nickname: Optional[str] = Field(default=None, min_length=1, max_length=64)
    model_id: Optional[str] = Field(default=None, min_length=1, max_length=128)
    vendor: Optional[str] = Field(default=None, min_length=1, max_length=64)
    model_type: Optional[str] = Field(default=None, min_length=1, max_length=64)
    remark: Optional[str] = None
    points_cost: Optional[int] = Field(default=None, ge=0)
    model_multiplier: Optional[Decimal] = Field(default=None, ge=0)
    cache_multiplier: Optional[Decimal] = Field(default=None, ge=0)
    completion_multiplier: Optional[Decimal] = Field(default=None, ge=0)
    platform_multiplier: Optional[Decimal] = Field(default=None, ge=0)
    is_enabled: Optional[bool] = None
    capabilities: Optional[Dict[str, Any]] = None


class ProviderModelOut(SchemaBaseModel):
    id: str
    model_id: str
    nickname: str
    vendor: str
    model_type: str
    points_cost: int = 0
    model_multiplier: Decimal = Decimal("1.0000")
    cache_multiplier: Decimal = Decimal("1.0000")
    completion_multiplier: Decimal = Decimal("1.0000")
    platform_multiplier: Decimal = Decimal("1.0000")
    is_enabled: bool = True
    capabilities: Dict[str, Any] = Field(default_factory=dict)
    object: Optional[str] = None
    owned_by: Optional[str] = None
    root: Optional[str] = None
    parent: Optional[str] = None


class ProviderModelImportItem(SchemaBaseModel):
    model_id: str = Field(..., min_length=1, max_length=128)
    nickname: Optional[str] = Field(default=None, max_length=64)
    vendor: Optional[str] = Field(default=None, max_length=64)
    model_type: str = Field(default="text", min_length=1, max_length=64)
    remark: Optional[str] = None
    points_cost: int = Field(default=0, ge=0)
    model_multiplier: Decimal = Field(default=Decimal("1.0000"), ge=0)
    cache_multiplier: Decimal = Field(default=Decimal("1.0000"), ge=0)
    completion_multiplier: Decimal = Field(default=Decimal("1.0000"), ge=0)
    platform_multiplier: Decimal = Field(default=Decimal("1.0000"), ge=0)
    is_enabled: bool = True
    capabilities: Dict[str, Any] = Field(default_factory=dict)


class ProviderModelImportRequest(SchemaBaseModel):
    models: List[ProviderModelImportItem]


class ProviderModelImportOut(SchemaBaseModel):
    created: List[AiModelOut]
    skipped: List[str]
