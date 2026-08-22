from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from pydantic import Field, field_validator

from app.schemas.base import SchemaBaseModel


class AgentBatchDispatchRequest(SchemaBaseModel):
    expected_core_asset_lock_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=128)
    max_tasks: int = Field(default=5, ge=1, le=50)

    @field_validator("idempotency_key")
    @classmethod
    def normalize_idempotency_key(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 8:
            raise ValueError("幂等键至少需要 8 个字符")
        return normalized


class AgentVideoModelSelectionRequest(SchemaBaseModel):
    expected_core_asset_lock_version: int = Field(ge=1)
    video_model_id: UUID
    video_resolution: Literal[
        "360p", "480p", "540p", "720p", "768P", "1080p", "2K", "4k"
    ]


class AgentBatchEpisodeOut(SchemaBaseModel):
    chapter_id: UUID
    episode_number: int
    title: str
    storyboard_analysis_status: str
    storyboard_count: int
    image_status_counts: Dict[str, int]
    video_status_counts: Dict[str, int]
    blocked_count: int


class AgentBatchProductionOut(SchemaBaseModel):
    production_id: UUID
    step_id: Optional[UUID] = None
    core_asset_lock_version: int
    phase: str
    status: str
    current_stage: str
    paused: bool
    remaining_episode_count: int
    episodes: List[AgentBatchEpisodeOut]
    storyboard_count: int
    image_status_counts: Dict[str, int]
    video_status_counts: Dict[str, int]
    active_task_count: int
    failed_item_count: int
    blocked_storyboard_count: int
    quality_issues: List[Dict[str, Any]]
    estimated_remaining_points: int
    submitted_points: int
    recommended_batch_size: int
    selected_video_model_id: Optional[UUID] = None
    requires_video_model: bool
    can_dispatch: bool
    is_complete: bool
