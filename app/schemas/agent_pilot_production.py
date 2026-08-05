from typing import Dict, List, Literal, Optional
from uuid import UUID

from pydantic import Field, field_validator

from app.schemas.base import SchemaBaseModel


class AgentPilotActionRequest(SchemaBaseModel):
    expected_core_asset_lock_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=128)

    @field_validator("idempotency_key")
    @classmethod
    def normalize_idempotency_key(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 8:
            raise ValueError("幂等键至少需要 8 个字符")
        return normalized


class AgentPilotIssueOut(SchemaBaseModel):
    severity: Literal["error", "warning", "info"]
    code: str
    message: str
    chapter_id: Optional[UUID] = None
    storyboard_id: Optional[UUID] = None


class AgentPilotEpisodeOut(SchemaBaseModel):
    chapter_id: UUID
    episode_number: int
    title: str
    storyboard_analysis_status: str
    storyboard_count: int
    image_status_counts: Dict[str, int]
    video_status_counts: Dict[str, int]


class AgentPilotProductionOut(SchemaBaseModel):
    production_id: UUID
    step_id: Optional[UUID] = None
    core_asset_lock_version: int
    pilot_episode_count: int
    phase: str
    status: str
    current_stage: str
    storyboard_checkpoint_id: Optional[UUID] = None
    final_checkpoint_id: Optional[UUID] = None
    episodes: List[AgentPilotEpisodeOut]
    storyboard_count: int
    image_status_counts: Dict[str, int]
    video_status_counts: Dict[str, int]
    issues: List[AgentPilotIssueOut]
    error_count: int
    warning_count: int
    submitted_points: int
    can_submit_images: bool
    can_submit_videos: bool
    can_confirm: bool
