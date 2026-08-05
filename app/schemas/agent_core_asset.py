from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from app.schemas.base import SchemaBaseModel
from app.schemas.agent_story_bible import AgentAssetSourceEvidence


CoreAssetType = Literal["character", "scene", "prop"]


class CoreAssetRef(SchemaBaseModel):
    asset_type: CoreAssetType
    asset_id: UUID


class CoreAssetImageGenerationItem(CoreAssetRef):
    generation_mode: str = Field(default="general", min_length=1, max_length=64)
    prompt: Optional[str] = Field(default=None, min_length=1)
    extra: Optional[Dict[str, Any]] = None


class CoreAssetImageGenerationRequest(SchemaBaseModel):
    ai_model_id: Optional[UUID] = None
    items: List[CoreAssetImageGenerationItem] = Field(min_length=1, max_length=50)

    @field_validator("items")
    @classmethod
    def distinct_items(
        cls,
        value: List[CoreAssetImageGenerationItem],
    ) -> List[CoreAssetImageGenerationItem]:
        keys = [(item.asset_type, item.asset_id) for item in value]
        if len(keys) != len(set(keys)):
            raise ValueError("核心资产不能重复")
        return value


class CoreAssetImageGenerationResult(CoreAssetRef):
    submitted: bool
    task_record_id: Optional[UUID] = None
    status: Optional[str] = None
    points_cost: int = 0
    next_poll_seconds: Optional[int] = None
    error_code: Optional[int] = None
    error_message: Optional[str] = None


class CoreAssetImageGenerationOut(SchemaBaseModel):
    items: List[CoreAssetImageGenerationResult]
    submitted_count: int
    failed_count: int
    total_points_cost: int


class CoreAssetReferenceImageRequest(SchemaBaseModel):
    expected_lock_version: int = Field(..., ge=0)
    reference_image: Optional[str] = Field(..., max_length=512)

    @field_validator("reference_image")
    @classmethod
    def normalize_reference_image(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return value.strip() or None


class CoreAssetReadinessItem(CoreAssetRef):
    candidate_id: UUID
    name: str
    aliases: List[str]
    reference_image: Optional[str] = None
    review_status: str
    image_generation_status: Optional[str] = None
    locked: bool = False


class CoreAssetReadinessOut(SchemaBaseModel):
    bible_version: int
    lock_version: int
    lock_status: Optional[str] = None
    items: List[CoreAssetReadinessItem]
    ready_count: int
    missing_reference_count: int
    can_lock: bool


class CoreAssetCreateRequest(SchemaBaseModel):
    asset_type: CoreAssetType
    canonical_name: str = Field(..., min_length=1, max_length=128)
    aliases: List[str] = Field(default_factory=list)
    content: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("canonical_name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("资产名称不能为空")
        return normalized

    @field_validator("aliases")
    @classmethod
    def normalize_aliases(cls, value: List[str]) -> List[str]:
        return list(dict.fromkeys(item.strip() for item in value if item.strip()))


class CoreAssetUpdateRequest(SchemaBaseModel):
    expected_lock_version: int = Field(..., ge=0)
    canonical_name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    aliases: Optional[List[str]] = None
    content: Optional[Dict[str, Any]] = None

    @field_validator("aliases")
    @classmethod
    def normalize_aliases(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        if value is None:
            return None
        return list(dict.fromkeys(item.strip() for item in value if item.strip()))

    @field_validator("content")
    @classmethod
    def require_non_empty_content(
        cls, value: Optional[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        if value is not None and not value:
            raise ValueError("资产内容不能为空")
        return value

    @field_validator("canonical_name")
    @classmethod
    def normalize_name(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("资产名称不能为空")
        return normalized

    @model_validator(mode="after")
    def require_update_field(self):
        if not self.model_dump(exclude={"expected_lock_version"}, exclude_none=True):
            raise ValueError("至少提供一个需要更新的字段")
        return self


class CoreAssetVariantCreateRequest(SchemaBaseModel):
    canonical_name: str = Field(..., min_length=1, max_length=128)
    variant_type: str = Field(..., min_length=1, max_length=64)
    description: str = Field(..., min_length=1)
    trigger_reason: str = Field(..., min_length=1)
    episode_numbers: List[int] = Field(default_factory=list)
    source_evidence: List[AgentAssetSourceEvidence] = Field(default_factory=list)
    content: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("canonical_name", "variant_type", "description", "trigger_reason")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("字段不能为空")
        return normalized

    @field_validator("episode_numbers")
    @classmethod
    def normalize_episode_numbers(cls, value: List[int]) -> List[int]:
        if any(number < 1 for number in value):
            raise ValueError("关联集数必须大于等于 1")
        return sorted(set(value))


class CoreAssetVariantOut(SchemaBaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    base_candidate_id: UUID
    asset_type: CoreAssetType
    canonical_name: str
    variant_type: str
    description: str
    trigger_reason: str
    episode_numbers: List[int]
    source_evidence: List[AgentAssetSourceEvidence]
    content: Dict[str, Any]
    reference_image: Optional[str] = None
    image_generation_status: Optional[str] = None
    lock_version: int
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="before")
    @classmethod
    def load_generation_status(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return value
        extra = getattr(value, "extra", None) or {}
        return {
            "id": value.id,
            "base_candidate_id": value.base_candidate_id,
            "asset_type": value.asset_type,
            "canonical_name": value.canonical_name,
            "variant_type": value.variant_type,
            "description": value.description,
            "trigger_reason": value.trigger_reason,
            "episode_numbers": value.episode_numbers,
            "source_evidence": value.source_evidence,
            "content": value.content,
            "reference_image": value.reference_image,
            "image_generation_status": extra.get("image_generation_status"),
            "lock_version": value.lock_version,
            "created_at": value.created_at,
            "updated_at": value.updated_at,
        }


class CoreAssetVariantImageGenerationRequest(SchemaBaseModel):
    prompt: Optional[str] = Field(default=None, min_length=1)
    extra: Optional[Dict[str, Any]] = None


class CoreAssetVariantImageGenerationOut(CoreAssetRef):
    variant_id: UUID
    submitted: bool
    task_record_id: Optional[UUID] = None
    status: Optional[str] = None
    points_cost: int = 0
    next_poll_seconds: Optional[int] = None


class CoreAssetManageItem(CoreAssetRef):
    candidate_id: UUID
    canonical_name: str
    aliases: List[str]
    content: Dict[str, Any]
    reference_image: Optional[str] = None
    image_generation_status: Optional[str] = None
    lock_version: int
    variants: List[CoreAssetVariantOut]
    created_at: datetime
    updated_at: datetime


class CoreAssetManageListOut(SchemaBaseModel):
    items: List[CoreAssetManageItem]
    total: int
    page: int
    page_size: int


class CoreAssetDeleteOut(CoreAssetRef):
    deleted: bool
    deleted_variant_count: int


class CoreAssetVariantDeleteOut(SchemaBaseModel):
    variant_id: UUID
    deleted: bool


class CoreAssetSelectionRequest(SchemaBaseModel):
    expected_lock_version: int = Field(ge=0)
    assets: List[CoreAssetRef] = Field(min_length=2, max_length=100)

    @field_validator("assets")
    @classmethod
    def distinct_assets(cls, value: List[CoreAssetRef]) -> List[CoreAssetRef]:
        keys = [(item.asset_type, item.asset_id) for item in value]
        if len(keys) != len(set(keys)):
            raise ValueError("核心资产不能重复")
        return value


class CoreAssetImpactChange(CoreAssetRef):
    name: str
    change_type: Literal["added", "removed", "reference_changed"]
    previous_reference_image: Optional[str] = None
    proposed_reference_image: Optional[str] = None


class CoreAssetImpactOut(SchemaBaseModel):
    current_lock_version: int
    proposed_lock_version: int
    impact_fingerprint: str
    changes: List[CoreAssetImpactChange]
    affected_storyboard_ids: List[UUID]
    affected_storyboard_count: int
    affected_image_count: int
    affected_video_count: int
    warnings: List[str]


class CoreAssetLockRequest(CoreAssetSelectionRequest):
    idempotency_key: str = Field(min_length=8, max_length=128)
    impact_fingerprint: Optional[str] = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern="^[0-9a-f]{64}$",
    )

    @field_validator("idempotency_key")
    @classmethod
    def normalize_idempotency_key(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 8:
            raise ValueError("幂等键至少需要 8 个字符")
        return normalized


class CoreAssetConfirmRequest(SchemaBaseModel):
    expected_lock_version: int = Field(..., ge=0)
    idempotency_key: str = Field(..., min_length=8, max_length=128)
    impact_fingerprint: Optional[str] = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern="^[0-9a-f]{64}$",
    )

    @field_validator("idempotency_key")
    @classmethod
    def normalize_idempotency_key(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 8:
            raise ValueError("幂等键至少需要 8 个字符")
        return normalized


class CoreAssetLockOut(SchemaBaseModel):
    lock_id: UUID
    version: int
    status: str
    assets: List[Dict[str, Any]]
    impact: Dict[str, Any]
    already_locked: bool
