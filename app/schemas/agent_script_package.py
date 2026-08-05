from typing import Any, Dict, List
from uuid import UUID

from pydantic import Field, field_validator

from app.schemas.agent_production import AgentEpisodePlanItemOut
from app.schemas.agent_story_bible import AgentAssetCandidateOut, AgentAssetVariantOut
from app.schemas.base import SchemaBaseModel


class AgentScriptPackageOut(SchemaBaseModel):
    production_id: UUID
    script_version: int
    bible_version: int
    status: str
    episodes: List[AgentEpisodePlanItemOut] = Field(description="自动拆分后的剧集规划")
    characters: List[AgentAssetCandidateOut] = Field(
        description="人物基础资产；每项 asset_type 固定为 character"
    )
    character_variants: List[AgentAssetVariantOut] = Field(
        description="人物变装或状态变体；只能关联 characters 中的人物基础资产"
    )
    scenes: List[AgentAssetCandidateOut] = Field(
        description="场景基础资产；每项 asset_type 固定为 scene"
    )
    scene_variants: List[AgentAssetVariantOut] = Field(
        description="场景时间、天气、季节、损坏等变体；只能关联 scenes 中的场景基础资产"
    )
    props: List[AgentAssetCandidateOut] = Field(
        description="道具基础资产；每项 asset_type 固定为 prop"
    )
    prop_variants: List[AgentAssetVariantOut] = Field(
        description="道具形态、损坏、开合、归属等变体；只能关联 props 中的道具基础资产"
    )
    warnings: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="阻止确认或需要前端提示的结构警告",
    )


class AgentScriptPackageConfirmRequest(SchemaBaseModel):
    expected_script_version: int = Field(..., ge=1)
    expected_bible_version: int = Field(..., ge=1)
    idempotency_key: str = Field(..., min_length=8, max_length=128)

    @field_validator("idempotency_key")
    @classmethod
    def normalize_idempotency_key(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 8:
            raise ValueError("幂等键至少需要 8 个字符")
        return normalized


class AgentScriptPackageConfirmOut(SchemaBaseModel):
    production_id: UUID
    script_version: int
    bible_version: int
    chapter_ids: List[UUID]
    asset_ids: List[UUID]
    created_chapter_count: int
    created_asset_count: int
    reused_asset_count: int
    already_confirmed: bool


class AgentScriptSupplementRequest(SchemaBaseModel):
    content: str = Field(..., min_length=1)

    @field_validator("content")
    @classmethod
    def normalize_content(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("补充剧本内容不能为空")
        return normalized


class AgentScriptSupplementOut(SchemaBaseModel):
    production_id: UUID
    source_document_id: UUID
    source_version: int
    step_id: UUID
    appended_character_count: int
    status: str
    current_stage: str
