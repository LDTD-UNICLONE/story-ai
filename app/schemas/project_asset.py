from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import ConfigDict, Field, field_serializer, field_validator

from app.core.public_messages import sanitize_public_data
from app.schemas.base import SchemaBaseModel


class ProjectAssetBaseOut(SchemaBaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    user_id: UUID
    source_chapter_id: Optional[UUID] = None
    name: str
    description: Optional[str] = None
    prompt: Optional[str] = None
    reference_image: Optional[str] = None
    source_content: Optional[str] = None
    extra: Dict[str, Any]
    is_enabled: bool
    created_at: datetime
    updated_at: datetime

    @field_serializer("extra")
    def serialize_extra(self, value: Dict[str, Any]) -> Dict[str, Any]:
        return sanitize_public_data(value)


class ProjectCharacterOut(ProjectAssetBaseOut):
    aliases: List[str]
    identity: Optional[str] = None
    gender: Optional[str] = None
    age: Optional[str] = None
    appearance: Optional[str] = None
    personality: Optional[str] = None
    relationship: Optional[str] = None
    costume: Optional[str] = None


class ProjectSceneOut(ProjectAssetBaseOut):
    location: Optional[str] = None
    time_of_day: Optional[str] = None
    environment: Optional[str] = None
    atmosphere: Optional[str] = None


class ProjectPropOut(ProjectAssetBaseOut):
    category: Optional[str] = None
    appearance: Optional[str] = None
    function: Optional[str] = None


class ProjectCharacterListOut(SchemaBaseModel):
    items: List[ProjectCharacterOut]
    total: int
    page: int
    page_size: int


class ProjectSceneListOut(SchemaBaseModel):
    items: List[ProjectSceneOut]
    total: int
    page: int
    page_size: int


class ProjectPropListOut(SchemaBaseModel):
    items: List[ProjectPropOut]
    total: int
    page: int
    page_size: int


class ProjectAssetOptionOut(SchemaBaseModel):
    id: UUID
    asset_type: str
    name: str
    reference_image: Optional[str] = None
    source_chapter_id: Optional[UUID] = None
    aliases: Optional[List[str]] = None
    identity: Optional[str] = None
    location: Optional[str] = None
    time_of_day: Optional[str] = None
    category: Optional[str] = None


class ProjectAssetOptionsOut(SchemaBaseModel):
    items: List[ProjectAssetOptionOut]
    total: int


class ProjectCharacterCreateRequest(SchemaBaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    aliases: List[str] = Field(default_factory=list)
    identity: Optional[str] = Field(default=None, max_length=128)
    gender: Optional[str] = Field(default=None, max_length=32)
    age: Optional[str] = Field(default=None, max_length=64)
    appearance: Optional[str] = None
    personality: Optional[str] = None
    relationship: Optional[str] = None
    costume: Optional[str] = None
    description: Optional[str] = None
    prompt: Optional[str] = None
    reference_image: Optional[str] = Field(default=None, max_length=512)
    source_chapter_id: Optional[UUID] = None
    source_content: Optional[str] = None
    extra: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return _normalize_asset_name(value)


class ProjectCharacterUpdateRequest(SchemaBaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    aliases: Optional[List[str]] = None
    identity: Optional[str] = Field(default=None, max_length=128)
    gender: Optional[str] = Field(default=None, max_length=32)
    age: Optional[str] = Field(default=None, max_length=64)
    appearance: Optional[str] = None
    personality: Optional[str] = None
    relationship: Optional[str] = None
    costume: Optional[str] = None
    description: Optional[str] = None
    prompt: Optional[str] = None
    reference_image: Optional[str] = Field(default=None, max_length=512)
    source_chapter_id: Optional[UUID] = None
    source_content: Optional[str] = None
    extra: Optional[Dict[str, Any]] = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: Optional[str]) -> str:
        return _normalize_optional_asset_name(value)

    @field_validator("aliases", "extra")
    @classmethod
    def reject_null_json_fields(cls, value):
        return _reject_null_json_field(value)


class ProjectSceneCreateRequest(SchemaBaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    location: Optional[str] = Field(default=None, max_length=255)
    time_of_day: Optional[str] = Field(default=None, max_length=64)
    environment: Optional[str] = None
    atmosphere: Optional[str] = None
    description: Optional[str] = None
    prompt: Optional[str] = None
    reference_image: Optional[str] = Field(default=None, max_length=512)
    source_chapter_id: Optional[UUID] = None
    source_content: Optional[str] = None
    extra: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return _normalize_asset_name(value)


class ProjectSceneUpdateRequest(SchemaBaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    location: Optional[str] = Field(default=None, max_length=255)
    time_of_day: Optional[str] = Field(default=None, max_length=64)
    environment: Optional[str] = None
    atmosphere: Optional[str] = None
    description: Optional[str] = None
    prompt: Optional[str] = None
    reference_image: Optional[str] = Field(default=None, max_length=512)
    source_chapter_id: Optional[UUID] = None
    source_content: Optional[str] = None
    extra: Optional[Dict[str, Any]] = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: Optional[str]) -> str:
        return _normalize_optional_asset_name(value)

    @field_validator("extra")
    @classmethod
    def reject_null_extra(cls, value):
        return _reject_null_json_field(value)


class ProjectPropCreateRequest(SchemaBaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    category: Optional[str] = Field(default=None, max_length=64)
    appearance: Optional[str] = None
    function: Optional[str] = None
    description: Optional[str] = None
    prompt: Optional[str] = None
    reference_image: Optional[str] = Field(default=None, max_length=512)
    source_chapter_id: Optional[UUID] = None
    source_content: Optional[str] = None
    extra: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return _normalize_asset_name(value)


class ProjectPropUpdateRequest(SchemaBaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    category: Optional[str] = Field(default=None, max_length=64)
    appearance: Optional[str] = None
    function: Optional[str] = None
    description: Optional[str] = None
    prompt: Optional[str] = None
    reference_image: Optional[str] = Field(default=None, max_length=512)
    source_chapter_id: Optional[UUID] = None
    source_content: Optional[str] = None
    extra: Optional[Dict[str, Any]] = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: Optional[str]) -> str:
        return _normalize_optional_asset_name(value)

    @field_validator("extra")
    @classmethod
    def reject_null_extra(cls, value):
        return _reject_null_json_field(value)


def _normalize_asset_name(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("资产名称不能为空")
    return value


def _normalize_optional_asset_name(value: Optional[str]) -> str:
    if value is None:
        raise ValueError("资产名称不能为 null")
    return _normalize_asset_name(value)


def _reject_null_json_field(value):
    if value is None:
        raise ValueError("字段不能为 null")
    return value


class ProjectAssetAnalyzeRequest(SchemaBaseModel):
    ai_model_id: UUID
    analysis_prompt: Optional[str] = Field(default=None, min_length=1)
    extra: Optional[Dict[str, Any]] = None


class ProjectAssetAnalyzeOut(SchemaBaseModel):
    task_record_id: UUID
    asset_type: str
    status: str
    points_cost: int
    next_poll_seconds: Optional[int] = None


class ProjectAssetImageGenerateRequest(SchemaBaseModel):
    ai_model_id: UUID
    generation_mode: str = Field(default="general", min_length=1, max_length=64)
    prompt: Optional[str] = Field(default=None, min_length=1)
    extra: Optional[Dict[str, Any]] = None


class ProjectAssetImageGenerateOut(SchemaBaseModel):
    task_record_id: UUID
    asset_type: str
    asset_id: UUID
    status: str
    points_cost: int
    next_poll_seconds: Optional[int] = None
