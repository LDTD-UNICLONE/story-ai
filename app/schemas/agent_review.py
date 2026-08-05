from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from app.schemas.base import SchemaBaseModel


ReviewCategory = Literal[
    "character_consistency",
    "style_drift",
    "motion",
    "rhythm",
    "dialogue",
    "audio",
    "compliance",
    "other",
]


class AgentReviewIssueCreateRequest(SchemaBaseModel):
    chapter_id: UUID
    storyboard_id: Optional[UUID] = None
    media_type: Optional[Literal["image", "video", "audio", "text"]] = None
    category: ReviewCategory
    severity: Literal["info", "warning", "blocking"] = "warning"
    description: str = Field(min_length=2, max_length=2000)


class AgentReviewIssueUpdateRequest(SchemaBaseModel):
    expected_lock_version: int = Field(ge=0)
    status: Literal["resolved", "dismissed"]
    resolution_note: str = Field(min_length=2, max_length=2000)


class AgentReviewIssueOut(SchemaBaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    production_id: UUID
    chapter_id: UUID
    storyboard_id: Optional[UUID] = None
    media_type: Optional[str] = None
    category: str
    severity: str
    status: str
    description: str
    resolution_note: Optional[str] = None
    resolved_by: Optional[UUID] = None
    resolved_at: Optional[datetime] = None
    lock_version: int
    created_at: datetime
    updated_at: datetime


class AgentReviewStoryboardOut(SchemaBaseModel):
    storyboard_id: UUID
    shot_number: int
    title: str
    duration_seconds: int
    image_status: str
    image_url: Optional[str] = None
    image_history_id: Optional[UUID] = None
    image_version_count: int
    video_status: str
    video_url: Optional[str] = None
    video_history_id: Optional[UUID] = None
    video_version_count: int
    issue_count: int
    blocking_issue_count: int


class AgentEpisodeVideoTimelineItemOut(SchemaBaseModel):
    storyboard_id: UUID
    group_number: int = Field(ge=1)
    title: str
    estimated_duration_seconds: int = Field(ge=1)
    video_status: str
    video_url: Optional[str] = None
    video_history_id: Optional[UUID] = None


class AgentEpisodeVideoTimelineOut(SchemaBaseModel):
    production_id: UUID
    chapter_id: UUID
    episode_number: int = Field(ge=1)
    title: str
    review_status: Literal["pending", "approved", "invalidated"]
    review_lock_version: int = Field(ge=0)
    ready_to_approve: bool
    estimated_duration_seconds: int = Field(ge=0)
    items: List[AgentEpisodeVideoTimelineItemOut]


class AgentReviewEpisodeOut(SchemaBaseModel):
    chapter_id: UUID
    episode_number: int
    title: str
    storyboard_analysis_status: str
    can_review: bool
    review_status: Literal["pending", "approved", "invalidated"]
    review_lock_version: int
    approved_at: Optional[datetime] = None
    issue_count: int
    blocking_issue_count: int
    storyboards: List[AgentReviewStoryboardOut]


class AgentProductionReviewOut(SchemaBaseModel):
    production_id: UUID
    status: str
    total_episode_count: int
    approved_episode_count: int
    open_issue_count: int
    blocking_issue_count: int
    delivery_ready: bool
    episodes: List[AgentReviewEpisodeOut]


class AgentMediaRegeneratePreviewRequest(SchemaBaseModel):
    media_type: Literal["image", "video"]
    storyboard_ids: List[UUID] = Field(min_length=1, max_length=20)
    prompt: Optional[str] = Field(default=None, min_length=1, max_length=4000)

    @field_validator("storyboard_ids")
    @classmethod
    def unique_storyboard_ids(cls, value: List[UUID]) -> List[UUID]:
        return list(dict.fromkeys(value))


class AgentMediaRegenerateRequest(AgentMediaRegeneratePreviewRequest):
    idempotency_key: str = Field(min_length=8, max_length=128)


class AgentMediaRegeneratePreviewOut(SchemaBaseModel):
    production_id: UUID
    media_type: str
    storyboard_ids: List[UUID]
    affected_chapter_ids: List[UUID]
    invalidated_approval_count: int
    estimated_points: int


class AgentMediaRegenerationOut(SchemaBaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    production_id: UUID
    media_type: str
    idempotency_key: str
    status: str
    items: List[Dict[str, Any]]
    estimated_points: int
    submitted_points: int
    error_summary: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class AgentMediaVersionSelectRequest(SchemaBaseModel):
    result_url: Optional[str] = Field(default=None, max_length=2048)


class AgentMediaVersionSelectOut(SchemaBaseModel):
    history_id: UUID
    storyboard_id: UUID
    media_type: str
    selected_url: Optional[str] = None
    invalidated_chapter_id: UUID


class AgentEpisodeApproveRequest(SchemaBaseModel):
    expected_lock_version: int = Field(ge=0)
    idempotency_key: str = Field(min_length=8, max_length=128)


class AgentEpisodeApprovalOut(SchemaBaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    production_id: UUID
    chapter_id: UUID
    status: str
    media_snapshot_hash: str
    approved_by: UUID
    approved_at: datetime
    lock_version: int
    idempotency_key: str


class AgentDeliveryReadinessOut(SchemaBaseModel):
    production_id: UUID
    ready: bool
    total_episode_count: int
    approved_episode_count: int
    unapproved_chapter_ids: List[UUID]
    open_blocking_issue_count: int
    missing_video_storyboard_ids: List[UUID]


class AgentDeliveryCreateRequest(SchemaBaseModel):
    delivery_type: Literal["manifest", "merged_video", "jianying_draft"] = "manifest"
    chapter_ids: List[UUID] = Field(default_factory=list, max_length=200)
    platform: Optional[Literal["windows", "macos"]] = None
    jianying_version: Optional[Literal["10.8"]] = None
    draft_name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    idempotency_key: str = Field(min_length=8, max_length=128)

    @field_validator("chapter_ids")
    @classmethod
    def unique_chapter_ids(cls, value: List[UUID]) -> List[UUID]:
        return list(dict.fromkeys(value))

    @field_validator("draft_name")
    @classmethod
    def normalize_draft_name(cls, value: Optional[str]) -> Optional[str]:
        normalized = value.strip() if value else None
        return normalized or None

    @model_validator(mode="after")
    def validate_jianying_options(self):
        if self.delivery_type == "jianying_draft":
            if self.platform is None:
                raise ValueError("剪映草稿导出必须指定 platform")
            self.jianying_version = self.jianying_version or "10.8"
        elif self.platform is not None or self.jianying_version is not None or self.draft_name:
            raise ValueError("platform、jianying_version 和 draft_name 仅用于剪映草稿导出")
        return self


class AgentJianyingExportCreateRequest(SchemaBaseModel):
    platform: Literal["windows", "macos"]
    jianying_version: Literal["10.8"] = "10.8"
    draft_name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    chapter_ids: List[UUID] = Field(default_factory=list, max_length=200)
    idempotency_key: str = Field(min_length=8, max_length=128)

    @field_validator("chapter_ids")
    @classmethod
    def unique_chapter_ids(cls, value: List[UUID]) -> List[UUID]:
        return list(dict.fromkeys(value))

    @field_validator("draft_name")
    @classmethod
    def normalize_draft_name(cls, value: Optional[str]) -> Optional[str]:
        normalized = value.strip() if value else None
        return normalized or None


class AgentJianyingExportOut(SchemaBaseModel):
    id: UUID
    production_id: UUID
    status: str
    platform: Literal["windows", "macos"]
    jianying_version: str
    draft_name: str
    episode_count: int = Field(ge=0)
    video_count: int = Field(ge=0)
    output_url: Optional[str] = None
    error_summary: Optional[str] = None
    compatibility_notes: List[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class AgentDeliveryOut(SchemaBaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    production_id: UUID
    project_id: UUID
    delivery_type: str
    status: str
    idempotency_key: str
    manifest: Dict[str, Any]
    output_url: Optional[str] = None
    error_summary: Optional[str] = None
    lease_expires_at: Optional[datetime] = None
    attempt_count: int
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class AgentDeliveryListOut(SchemaBaseModel):
    items: List[AgentDeliveryOut]
