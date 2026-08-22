from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from pydantic import Field, field_validator

from app.schemas.base import SchemaBaseModel


class AgentEpisodeVideoGenerationRequest(SchemaBaseModel):
    expected_core_asset_lock_version: int = Field(ge=1)
    expected_episode_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=128)
    video_model_id: UUID
    video_resolution: Literal[
        "360p", "480p", "540p", "720p", "768P", "1080p", "2K", "4k"
    ] = "720p"

    @field_validator("idempotency_key")
    @classmethod
    def normalize_idempotency_key(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 8:
            raise ValueError("幂等键至少需要 8 个字符")
        return normalized


class AgentStoryboardVideoGenerationRequest(AgentEpisodeVideoGenerationRequest):
    expected_storyboard_revision: int = Field(ge=1)
    expected_video_config_version: int = Field(ge=0)
    duration_seconds: int = Field(ge=4, le=15)
    prompt: Optional[str] = Field(default=None, min_length=1, max_length=5000)

    @field_validator("prompt")
    @classmethod
    def normalize_prompt(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("视频补充提示词不能为空")
        return normalized


class AgentStoryboardVideoConfigRequest(SchemaBaseModel):
    expected_core_asset_lock_version: int = Field(ge=1)
    expected_storyboard_revision: int = Field(ge=1)
    expected_config_version: int = Field(ge=0)
    video_model_id: UUID
    video_resolution: Literal[
        "360p", "480p", "540p", "720p", "768P", "1080p", "2K", "4k"
    ] = "720p"
    estimated_duration_seconds: int = Field(ge=4, le=15)


class AgentStoryboardVideoConfigOut(SchemaBaseModel):
    storyboard_id: UUID
    chapter_id: UUID
    storyboard_revision: int = Field(ge=1)
    episode_revision: int = Field(ge=1)
    config_version: int = Field(ge=1)
    video_model_id: UUID
    video_resolution: str
    estimated_duration_seconds: int = Field(ge=4, le=15)


class AgentStoryboardVideoVersionOut(SchemaBaseModel):
    history_id: UUID
    task_record_id: Optional[UUID] = None
    video_model_id: Optional[UUID] = None
    result_url: Optional[str] = None
    result_urls: List[str] = Field(default_factory=list)
    last_frame_url: Optional[str] = None
    status: str
    is_selected: bool
    validity_status: str
    resolution: Optional[str] = None
    requested_duration_seconds: Optional[int] = None
    prompt: Optional[str] = None
    asset_bindings: List[Dict[str, Any]] = Field(default_factory=list)
    reference_images: List[str] = Field(default_factory=list)
    reference_manifest: List[Dict[str, Any]] = Field(default_factory=list)
    provider_parameters: Dict[str, Any] = Field(default_factory=dict)
    storyboard_revision: Optional[int] = None
    core_asset_lock_version: Optional[int] = None
    created_at: datetime


class AgentStoryboardVideoVersionsOut(SchemaBaseModel):
    production_id: UUID
    chapter_id: UUID
    storyboard_id: UUID
    selection_revision: int = Field(ge=0)
    selection_required: bool
    selected_history_id: Optional[UUID] = None
    latest_history_id: Optional[UUID] = None
    items: List[AgentStoryboardVideoVersionOut]
    total: int


class AgentStoryboardPrimaryVideoRequest(SchemaBaseModel):
    expected_selection_revision: int = Field(ge=0)
    history_id: UUID
    result_url: Optional[str] = Field(default=None, max_length=2048)


class AgentStoryboardPrimaryVideoOut(SchemaBaseModel):
    storyboard_id: UUID
    chapter_id: UUID
    history_id: UUID
    selected_url: Optional[str] = None
    selection_revision: int = Field(ge=1)
    selection_required: bool


class AgentStoryboardVideoTaskOut(SchemaBaseModel):
    status: str
    result_url: Optional[str] = None
    task_record_id: Optional[UUID] = None
    model_id: Optional[UUID] = None
    resolution: Optional[str] = None
    next_poll_seconds: Optional[int] = None
    selected_history_id: Optional[UUID] = None
    latest_history_id: Optional[UUID] = None
    selection_revision: int = Field(default=0, ge=0)
    selection_required: bool = False


class AgentEpisodeStoryboardVideoOut(SchemaBaseModel):
    storyboard_id: UUID
    shot_number: int
    title: str
    revision: int = Field(ge=1)
    estimated_duration_seconds: int = Field(ge=1)
    video: AgentStoryboardVideoTaskOut


class AgentEpisodeVideosOut(SchemaBaseModel):
    production_id: UUID
    chapter_id: UUID
    episode_number: int
    title: str
    episode_revision: int = Field(ge=1)
    core_asset_lock_version: int = Field(ge=1)
    status: Literal["ready", "processing", "selection_required", "failed", "completed"]
    storyboard_count: int
    video_status_counts: dict[str, int]
    active_task_count: int
    failed_item_count: int
    selection_required_count: int = 0
    can_generate: bool
    should_poll: bool
    next_poll_seconds: Optional[int] = None
    storyboards: List[AgentEpisodeStoryboardVideoOut]


class AgentEpisodeVideoGenerationResultItem(SchemaBaseModel):
    storyboard_id: UUID
    submitted: bool
    reused_active_task: bool = False
    task_record_id: Optional[UUID] = None
    status: str
    points_cost: int = 0
    next_poll_seconds: Optional[int] = None
    error_code: Optional[int] = None
    error_message: Optional[str] = None


class AgentEpisodeVideoGenerationOut(SchemaBaseModel):
    request_id: UUID
    production_id: UUID
    chapter_id: UUID
    idempotency_key: str
    status: Literal["pending", "submitted", "failed"]
    idempotent_replay: bool
    submitted_count: int
    reused_count: int
    failed_count: int
    total_points_cost: int
    items: List[AgentEpisodeVideoGenerationResultItem]
