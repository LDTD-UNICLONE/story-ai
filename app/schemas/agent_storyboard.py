from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.schemas.base import SchemaBaseModel


class AgentStoryboardGenerateRequest(SchemaBaseModel):
    expected_core_asset_lock_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=128)

    @field_validator("idempotency_key")
    @classmethod
    def normalize_idempotency_key(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 8:
            raise ValueError("幂等键至少需要 8 个字符")
        return normalized


class AgentStoryboardShotOut(SchemaBaseModel):
    shot_number: int = Field(ge=1)
    shot_size: str
    camera_shot: str
    camera_angle: str
    camera_movement: str
    visual_content: str
    scene_name: str
    characters: List[str]
    props: List[str]
    speaker: str
    dialogue: str
    character_binding_keys: List[str] = Field(default_factory=list)
    scene_binding_key: Optional[str] = None
    prop_binding_keys: List[str] = Field(default_factory=list)


class AgentStoryboardShotInput(SchemaBaseModel):
    shot_number: int = Field(ge=1)
    shot_size: str = Field(max_length=64)
    camera_shot: str = Field(max_length=2000)
    camera_angle: str = Field(max_length=128)
    camera_movement: str = Field(max_length=2000)
    visual_content: str = Field(max_length=5000)
    scene_name: str = Field(default="", max_length=128)
    characters: List[str] = Field(default_factory=list, max_length=50)
    props: List[str] = Field(default_factory=list, max_length=50)
    speaker: str = Field(default="", max_length=128)
    dialogue: str = Field(default="", max_length=5000)
    character_binding_keys: List[str] = Field(default_factory=list, max_length=50)
    scene_binding_key: Optional[str] = Field(default=None, max_length=64)
    prop_binding_keys: List[str] = Field(default_factory=list, max_length=50)


class AgentStoryboardAssetBinding(SchemaBaseModel):
    binding_key: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    asset_type: Literal["character", "scene", "prop"]
    asset_id: UUID
    variant_id: Optional[UUID] = None


class AgentStoryboardUpdateRequest(SchemaBaseModel):
    expected_core_asset_lock_version: int = Field(ge=1)
    expected_revision: int = Field(ge=1)
    title: Optional[str] = Field(default=None, min_length=1, max_length=128)
    source_content: Optional[str] = Field(default=None, max_length=20000)
    shots: Optional[List[AgentStoryboardShotInput]] = Field(default=None, max_length=50)
    prompt_notes: Optional[str] = Field(default=None, max_length=5000)
    storyboard_prompt: Optional[str] = Field(default=None, min_length=1, max_length=20000)
    estimated_duration_seconds: Optional[int] = Field(default=None, ge=4, le=15)
    asset_bindings: Optional[List[AgentStoryboardAssetBinding]] = Field(
        default=None,
        max_length=100,
    )

    @field_validator("storyboard_prompt")
    @classmethod
    def normalize_prompt(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip()
        required = ("画面风格：", "视频中不得出现任何字幕", "不要BGM", "不要配乐")
        if not all(item in normalized for item in required):
            raise ValueError("分镜提示词必须保留固定画风、纯画面和禁用配乐规则")
        return normalized

    @field_validator("asset_bindings")
    @classmethod
    def distinct_bindings(
        cls,
        value: Optional[List[AgentStoryboardAssetBinding]],
    ) -> Optional[List[AgentStoryboardAssetBinding]]:
        if value is None:
            return None
        asset_keys = [(item.asset_type, item.asset_id) for item in value]
        if len(asset_keys) != len(set(asset_keys)):
            raise ValueError("同一基础资产只能绑定一次")
        binding_keys = [item.binding_key for item in value if item.binding_key is not None]
        if len(binding_keys) != len(set(binding_keys)):
            raise ValueError("资产绑定槽位不能重复")
        return value

    @model_validator(mode="after")
    def require_change(self):
        if all(
            value is None
            for value in (
                self.title,
                self.source_content,
                self.shots,
                self.prompt_notes,
                self.storyboard_prompt,
                self.estimated_duration_seconds,
                self.asset_bindings,
            )
        ):
            raise ValueError("至少提交一个分镜组修改字段")
        if self.storyboard_prompt is not None and self.shots is not None:
            raise ValueError("完整提示词和结构化镜头不能同时修改")
        return self


class AgentStoryboardCreateRequest(SchemaBaseModel):
    expected_core_asset_lock_version: int = Field(ge=1)
    expected_episode_revision: Optional[int] = Field(default=None, ge=1)
    insert_after_storyboard_id: Optional[UUID] = None
    title: str = Field(default="新分镜组", min_length=1, max_length=128)
    source_content: str = Field(default="", max_length=20000)


class AgentStoryboardCopyRequest(SchemaBaseModel):
    expected_core_asset_lock_version: int = Field(ge=1)
    expected_revision: int = Field(ge=1)


class AgentStoryboardReorderRequest(SchemaBaseModel):
    expected_episode_revision: int = Field(ge=1)
    storyboard_ids: List[UUID] = Field(min_length=1, max_length=500)

    @field_validator("storyboard_ids")
    @classmethod
    def distinct_storyboard_ids(cls, value: List[UUID]) -> List[UUID]:
        if len(value) != len(set(value)):
            raise ValueError("分镜组 ID 不能重复")
        return value


class AgentStoryboardItemOut(SchemaBaseModel):
    id: UUID
    chapter_id: UUID
    shot_number: int
    group_number: int
    revision: int = Field(ge=1)
    status: Literal["draft", "ready", "invalid"]
    origin: Literal["model", "user", "copy"]
    title: str
    source_content: str
    event_goal: Optional[str] = None
    scene_name: Optional[str] = None
    scene_state: Optional[str] = None
    characters: List[str]
    props: List[str]
    shot_size: Optional[str] = None
    camera_angle: Optional[str] = None
    camera_movement: Optional[str] = None
    screen_execution: Optional[str] = None
    action: Optional[str] = None
    character_action: Optional[str] = None
    character_expression: Optional[str] = None
    dialogue: Optional[str] = None
    sound_effect: Optional[str] = None
    atmosphere: Optional[str] = None
    shots: List[AgentStoryboardShotOut]
    storyboard_prompt: str
    prompt_template: str
    effective_prompt: str
    prompt_notes: str
    estimated_duration_seconds: int = Field(ge=1)
    duration_source: Literal["model", "user"] = "model"
    video_config: Dict[str, Any] = Field(default_factory=dict)
    asset_ids: Dict[str, List[UUID]]
    asset_bindings: List[AgentStoryboardAssetBinding] = Field(default_factory=list)
    production_focus: Optional[str] = None
    ending_frame: Optional[str] = None
    validation_errors: List[str] = Field(default_factory=list)
    updated_at: datetime


class AgentStoryboardAssetVariantOptionOut(SchemaBaseModel):
    variant_id: UUID
    canonical_name: str
    variant_type: str
    description: str
    trigger_reason: str
    reference_image: Optional[str] = None
    selected: bool = False


class AgentStoryboardAssetOptionOut(SchemaBaseModel):
    asset_type: Literal["character", "scene", "prop"]
    asset_id: UUID
    candidate_id: UUID
    canonical_name: str
    reference_image: Optional[str] = None
    bound: bool = False
    binding_key: Optional[str] = None
    selected_variant_id: Optional[UUID] = None
    mention_text: Optional[str] = None
    mention_reference_image: Optional[str] = None
    mention_enabled: bool = False
    variants: List[AgentStoryboardAssetVariantOptionOut] = Field(default_factory=list)


class AgentStoryboardAssetOptionsOut(SchemaBaseModel):
    production_id: UUID
    chapter_id: UUID
    storyboard_id: UUID
    episode_number: int
    core_asset_lock_version: int = Field(ge=1)
    storyboard_revision: int = Field(ge=1)
    items: List[AgentStoryboardAssetOptionOut]


class AgentStoryboardEpisodeOut(SchemaBaseModel):
    chapter_id: UUID
    episode_number: int
    title: str
    status: str
    revision: int = Field(ge=1)
    task_record_id: Optional[UUID] = None
    estimated_duration_seconds: int = Field(ge=0)
    storyboards: List[AgentStoryboardItemOut]


class AgentStoryboardAnalysisEpisodeOut(SchemaBaseModel):
    chapter_id: UUID
    episode_number: int
    title: str
    status: str
    task_record_id: Optional[UUID] = None
    error: Optional[str] = None


class AgentStoryboardFailedEpisodeOut(AgentStoryboardAnalysisEpisodeOut):
    pass


class AgentStoryboardDeleteOut(SchemaBaseModel):
    id: UUID
    chapter_id: UUID
    deleted: bool
    episode_revision: int = Field(ge=1)


class AgentStoryboardOrderOut(SchemaBaseModel):
    chapter_id: UUID
    episode_revision: int = Field(ge=1)
    storyboards: List[AgentStoryboardItemOut]


class AgentStoryboardPackageOut(SchemaBaseModel):
    production_id: UUID
    core_asset_lock_version: int
    phase: str
    status: str
    current_stage: str
    episode_count: int
    completed_episode_count: int
    remaining_episode_count: int
    analysis_complete: bool
    episode_analyses: List[AgentStoryboardAnalysisEpisodeOut] = Field(default_factory=list)
    current_analysis: Optional[AgentStoryboardAnalysisEpisodeOut] = None
    failed_episodes: List[AgentStoryboardFailedEpisodeOut] = Field(default_factory=list)
    storyboard_count: int
    estimated_duration_seconds: int = Field(ge=0)
    active_task_count: int
    failed_item_count: int
    can_generate: bool
    episodes: List[AgentStoryboardEpisodeOut]
