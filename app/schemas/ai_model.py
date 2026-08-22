from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator
from app.schemas.base import SchemaBaseModel


AiModelType = Literal["text", "image", "video"]


def _required_text(value: object) -> object:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("字段不能为空")
    return value.strip()


def _optional_text(value: object) -> object:
    if isinstance(value, str):
        normalized = value.strip()
        return normalized or None
    return value


class AiModelOut(SchemaBaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    nickname: str
    model_id: str
    vendor: str
    model_type: str
    remark: Optional[str] = None
    is_enabled: bool
    is_agent_default: bool
    configuration: Dict[str, Any]
    created_at: datetime
    updated_at: datetime


class AiModelListOut(SchemaBaseModel):
    items: List[AiModelOut]
    total: int
    page: int
    page_size: int


class AiModelBillingRecommendationOut(SchemaBaseModel):
    ai_model_id: UUID
    model_id: str
    model_type: str
    vendor: str
    platform_rate: Decimal
    recommendation_basis: str
    evaluated_success_tasks: int
    sample_count: int
    confidence: Literal["none", "low", "medium", "high"]
    safe_to_apply: bool
    minimum_points: Optional[int] = None
    p50_points: Optional[int] = None
    p90_points: Optional[int] = None
    p95_points: Optional[int] = None
    maximum_points: Optional[int] = None
    recommended_base_points: Optional[int] = None
    recommended_precharge_points: Optional[int] = None
    current_base_points: int
    suggested_patch: Optional[Dict[str, Any]] = None


class AiModelOptionOut(SchemaBaseModel):
    id: UUID
    nickname: str
    model_id: str
    vendor: str
    model_type: str
    remark: Optional[str] = None
    configuration: Dict[str, Any]

    model_config = ConfigDict(from_attributes=True)


class AiModelCreateRequest(SchemaBaseModel):
    model_config = ConfigDict(extra="forbid")

    nickname: str = Field(..., min_length=1, max_length=64)
    model_id: str = Field(..., min_length=1, max_length=128)
    vendor: str = Field(..., min_length=1, max_length=64)
    model_type: AiModelType
    remark: Optional[str] = None
    is_enabled: bool = True
    is_agent_default: bool = False
    configuration: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("nickname", "model_id", "vendor", "model_type", mode="before")
    @classmethod
    def normalize_required_text(cls, value: object) -> object:
        return _required_text(value)

    @field_validator("remark", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: object) -> object:
        return _optional_text(value)

    @field_validator("configuration", mode="before")
    @classmethod
    def reject_null_configuration(cls, value: object) -> object:
        if value is None:
            raise ValueError("字段不能为 null")
        return value


class AiModelUpdateRequest(SchemaBaseModel):
    model_config = ConfigDict(extra="forbid")

    nickname: Optional[str] = Field(default=None, min_length=1, max_length=64)
    model_id: Optional[str] = Field(default=None, min_length=1, max_length=128)
    vendor: Optional[str] = Field(default=None, min_length=1, max_length=64)
    model_type: Optional[AiModelType] = None
    remark: Optional[str] = None
    is_enabled: Optional[bool] = None
    is_agent_default: Optional[bool] = None
    configuration: Optional[Dict[str, Any]] = None

    @field_validator("nickname", "model_id", "vendor", "model_type", mode="before")
    @classmethod
    def normalize_present_required_text(cls, value: object) -> object:
        if value is None:
            raise ValueError("字段不能为 null")
        return _required_text(value)

    @field_validator(
        "is_enabled",
        "is_agent_default",
        "configuration",
        mode="before",
    )
    @classmethod
    def reject_null_non_nullable_fields(cls, value: object) -> object:
        if value is None:
            raise ValueError("字段不能为 null")
        return value

    @field_validator("remark", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: object) -> object:
        return _optional_text(value)


class ProviderModelOut(SchemaBaseModel):
    id: str
    model_id: str
    nickname: str
    vendor: str
    model_type: str
    is_enabled: bool = True
    is_agent_default: bool = False
    configuration: Dict[str, Any] = Field(default_factory=dict)
    object: Optional[str] = None
    owned_by: Optional[str] = None
    root: Optional[str] = None
    parent: Optional[str] = None


class ProviderModelImportItem(SchemaBaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: str = Field(..., min_length=1, max_length=128)
    nickname: Optional[str] = Field(default=None, max_length=64)
    vendor: Optional[str] = Field(default=None, max_length=64)
    model_type: AiModelType = "text"
    remark: Optional[str] = None
    is_enabled: bool = True
    is_agent_default: bool = False
    configuration: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("model_id", "model_type", mode="before")
    @classmethod
    def normalize_required_text(cls, value: object) -> object:
        return _required_text(value)

    @field_validator("nickname", "vendor", "remark", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: object) -> object:
        return _optional_text(value)

    @field_validator("configuration", mode="before")
    @classmethod
    def reject_null_configuration(cls, value: object) -> object:
        if value is None:
            raise ValueError("字段不能为 null")
        return value


class ProviderModelImportRequest(SchemaBaseModel):
    models: List[ProviderModelImportItem] = Field(..., min_length=1, max_length=100)


class ProviderModelImportOut(SchemaBaseModel):
    created: List[AiModelOut]
    skipped: List[str]
