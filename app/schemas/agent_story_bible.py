from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from app.schemas.base import SchemaBaseModel


class SeriesBibleVersionOut(SchemaBaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    production_id: UUID
    step_id: UUID
    version: int
    status: str
    content: Dict[str, Any]
    created_by: Optional[UUID] = None
    confirmed_by: Optional[UUID] = None
    confirmed_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class SeriesBibleVersionListOut(SchemaBaseModel):
    items: List[SeriesBibleVersionOut]


class SeriesBibleUpdateRequest(SchemaBaseModel):
    expected_version: int = Field(..., ge=1)
    content: Dict[str, Any] = Field(..., min_length=1)


class AgentAssetCandidateOut(SchemaBaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    production_id: UUID
    bible_version_id: UUID
    asset_type: Literal["character", "scene", "prop"] = Field(
        description="基础资产分类：人物 character、场景 scene、道具 prop"
    )
    canonical_name: str
    aliases: List[str]
    episode_numbers: List[int] = Field(description="该基础资产在分集剧本中出现的集数")
    source_chapter_ids: List[UUID]
    confidence: float
    merge_reason: str
    review_status: str
    content: Dict[str, Any]
    materialized_asset_id: Optional[UUID] = None
    lock_version: int
    created_at: datetime
    updated_at: datetime


class AgentAssetCandidateListOut(SchemaBaseModel):
    bible_version: int
    items: List[AgentAssetCandidateOut]
    total: int


class AgentAssetSourceEvidence(SchemaBaseModel):
    source_start: int = Field(..., ge=0)
    source_end: int = Field(..., ge=1)
    source_quote: Optional[str] = None

    @model_validator(mode="after")
    def require_valid_range(self):
        if self.source_end <= self.source_start:
            raise ValueError("原文证据结束位置必须大于开始位置")
        return self


class AgentAssetVariantOut(SchemaBaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    production_id: UUID
    bible_version_id: UUID
    base_candidate_id: UUID
    asset_type: Literal["character", "scene", "prop"] = Field(
        description="变体所属资产分类，必须与基础资产 asset_type 一致"
    )
    canonical_name: str
    variant_type: str
    description: str
    trigger_reason: str
    episode_numbers: List[int]
    source_evidence: List[AgentAssetSourceEvidence]
    confidence: float
    review_status: str
    content: Dict[str, Any]
    lock_version: int
    created_at: datetime
    updated_at: datetime


class AgentAssetVariantUpdateRequest(SchemaBaseModel):
    expected_lock_version: int = Field(..., ge=0)
    canonical_name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    variant_type: Optional[str] = Field(default=None, min_length=1, max_length=64)
    description: Optional[str] = Field(default=None, min_length=1)
    trigger_reason: Optional[str] = Field(default=None, min_length=1)
    episode_numbers: Optional[List[int]] = None
    source_evidence: Optional[List[AgentAssetSourceEvidence]] = None
    content: Optional[Dict[str, Any]] = None
    review_status: Optional[Literal["ready", "rejected"]] = None

    @field_validator("episode_numbers")
    @classmethod
    def normalize_episode_numbers(cls, value: Optional[List[int]]) -> Optional[List[int]]:
        if value is None:
            return None
        if any(number < 1 for number in value):
            raise ValueError("关联集数必须大于等于 1")
        return sorted(set(value))

    @model_validator(mode="after")
    def require_update_field(self):
        if not self.model_dump(exclude={"expected_lock_version"}, exclude_none=True):
            raise ValueError("至少提供一个需要更新的字段")
        return self


class AgentAssetCandidateUpdateRequest(SchemaBaseModel):
    expected_lock_version: int = Field(..., ge=0)
    canonical_name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    aliases: Optional[List[str]] = None
    content: Optional[Dict[str, Any]] = None
    review_status: Optional[Literal["ready", "rejected"]] = None

    @model_validator(mode="after")
    def require_update_field(self):
        if not self.model_dump(exclude={"expected_lock_version"}, exclude_none=True):
            raise ValueError("至少提供一个需要更新的字段")
        return self


class AgentAssetCandidateMaterializeRequest(SchemaBaseModel):
    expected_bible_version: int = Field(..., ge=1)
    candidate_ids: List[UUID] = Field(..., min_length=1, max_length=300)

    @field_validator("candidate_ids")
    @classmethod
    def require_distinct_candidate_ids(cls, value: List[UUID]) -> List[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("候选资产标识不能重复")
        return value


class AgentAssetCandidateMaterializeOut(SchemaBaseModel):
    bible_version: int
    asset_ids: List[UUID]
    created_count: int
    reused_count: int


class SeriesBibleConfirmRequest(SchemaBaseModel):
    expected_version: int = Field(..., ge=1)
    idempotency_key: str = Field(..., min_length=8, max_length=128)

    @field_validator("idempotency_key")
    @classmethod
    def normalize_idempotency_key(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 8:
            raise ValueError("幂等键至少需要 8 个字符")
        return normalized


class SeriesBibleConfirmOut(SchemaBaseModel):
    bible_version: int
    status: str
    already_confirmed: bool
    materialized_asset_count: int
