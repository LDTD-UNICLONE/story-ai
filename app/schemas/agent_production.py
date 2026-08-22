from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from app.schemas.base import SchemaBaseModel
from app.schemas.project import ProjectGenerationRatio
from app.schemas.style import StyleBaseOut


AgentProductionMode = Literal["supervised", "automatic"]
AgentVideoResolution = Literal[
    "360p", "480p", "540p", "720p", "768P", "1080p", "2K", "4k"
]
AgentProductionAction = Literal["start", "pause", "resume", "cancel"]
AgentProductionStatus = Literal[
    "draft",
    "planning",
    "waiting_approval",
    "running",
    "paused",
    "partially_failed",
    "completed",
    "cancelled",
]


class AgentProductionSpec(SchemaBaseModel):
    text_model_id: UUID
    image_model_id: UUID
    video_model_id: UUID
    target_episode_count: Optional[int] = Field(default=None, ge=1, le=200)
    target_episode_duration_seconds: int = Field(default=90, ge=15, le=600)
    default_shot_duration_seconds: int = Field(default=5, ge=4, le=15)
    video_resolution: AgentVideoResolution = "720p"
    generate_audio: bool = False
    retry_limit: int = Field(default=1, ge=0, le=5)
    pilot_episode_count: int = Field(default=1, ge=1, le=3)

    @model_validator(mode="after")
    def require_distinct_model_types(self):
        model_ids = {self.text_model_id, self.image_model_id, self.video_model_id}
        if len(model_ids) != 3:
            raise ValueError("文本、图像和视频模型不能使用同一个模型记录")
        return self


class AgentProductionFromTextRequest(SchemaBaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, max_length=128)
    content: str = Field(min_length=1)
    style_id: UUID
    generation_ratio: ProjectGenerationRatio
    video_resolution: AgentVideoResolution
    mode: AgentProductionMode
    video_model_id: Optional[UUID] = None

    @field_validator("name", "content")
    @classmethod
    def normalize_text(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("字段不能为空")
        return normalized

    @model_validator(mode="after")
    def validate_video_model_selection(self):
        _validate_initial_video_model_selection(self.mode, self.video_model_id)
        return self


class AgentProductionCreateRequest(SchemaBaseModel):
    source_type: Literal["text", "txt", "md", "docx", "pdf"] = "text"
    content: Optional[str] = Field(default=None, min_length=1)
    source_document_id: Optional[UUID] = None
    file_url: Optional[str] = Field(default=None, max_length=1024)
    file_name: Optional[str] = Field(default=None, max_length=255)
    mode: AgentProductionMode = "supervised"
    production_spec: AgentProductionSpec
    max_points: Optional[int] = Field(default=None, ge=0)

    @field_validator("content")
    @classmethod
    def normalize_content(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        content = value.strip()
        if not content:
            raise ValueError("整剧剧本内容不能为空")
        return content

    @model_validator(mode="after")
    def require_one_source(self):
        if bool(self.content) == bool(self.source_document_id):
            raise ValueError("content 和 source_document_id 必须且只能提供一个")
        return self


class ProjectSourceDocumentOut(SchemaBaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    source_type: str
    file_url: Optional[str] = None
    file_name: Optional[str] = None
    content_hash: str
    character_count: int
    version: int
    parse_status: str
    created_at: datetime
    updated_at: datetime


class AgentSourcePreviewOut(ProjectSourceDocumentOut):
    content_preview: str
    content_preview_truncated: bool
    warnings: List[str] = Field(default_factory=list)


class AgentStepOut(SchemaBaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    stage: str
    scope_type: str
    scope_id: UUID
    status: str
    input_version: int
    output_version: Optional[int] = None
    progress_current: int
    progress_total: int
    attempt_count: int
    error_summary: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    extra: Dict[str, Any]
    created_at: datetime
    updated_at: datetime


class AgentCheckpointOut(SchemaBaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    step_id: UUID
    checkpoint_type: str
    status: str
    summary: str
    impact: Dict[str, Any]
    approved_by: Optional[UUID] = None
    approved_at: Optional[datetime] = None
    rejected_reason: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class AgentProductionOut(SchemaBaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    source_document_id: UUID
    status: AgentProductionStatus
    current_stage: str
    mode: AgentProductionMode
    production_spec: Dict[str, Any]
    estimated_points: int
    consumed_points: int
    max_points: Optional[int] = None
    error_summary: Optional[str] = None
    lock_version: int
    created_at: datetime
    updated_at: datetime


class AgentProductionDetailOut(AgentProductionOut):
    source_document: ProjectSourceDocumentOut
    steps: List[AgentStepOut]
    checkpoints: List[AgentCheckpointOut]


class AgentProductionListOut(SchemaBaseModel):
    items: List[AgentProductionOut]
    total: int
    page: int
    page_size: int


class AgentProductionSourceOut(SchemaBaseModel):
    source_type: str
    file_name: Optional[str] = None
    content_hash: str
    character_count: int
    content_preview: str
    content_preview_truncated: bool
    warnings: List[str] = Field(default_factory=list)


class AgentProductionCreatedOut(SchemaBaseModel):
    production_id: UUID
    name: str
    status: AgentProductionStatus
    current_stage: str
    source: AgentProductionSourceOut
    style_id: UUID
    generation_ratio: ProjectGenerationRatio
    video_resolution: AgentVideoResolution
    mode: AgentProductionMode
    video_model_id: Optional[UUID] = None
    configuration_required: bool
    created_at: datetime


class AgentProductionConfigurationRequest(SchemaBaseModel):
    model_config = ConfigDict(extra="forbid")

    style_id: UUID
    generation_ratio: ProjectGenerationRatio
    video_resolution: AgentVideoResolution
    mode: AgentProductionMode
    video_model_id: Optional[UUID] = None

    @model_validator(mode="after")
    def validate_video_model_selection(self):
        _validate_initial_video_model_selection(self.mode, self.video_model_id)
        return self


class AgentProductionConfigurationOut(SchemaBaseModel):
    production_id: UUID
    style_id: Optional[UUID] = None
    style: Optional[StyleBaseOut] = None
    generation_ratio: Optional[ProjectGenerationRatio] = None
    video_resolution: Optional[AgentVideoResolution] = None
    mode: AgentProductionMode
    video_model_id: Optional[UUID] = None
    configured: bool
    configurable: bool


class AgentProductionDeletedOut(SchemaBaseModel):
    production_id: UUID
    status: AgentProductionStatus
    deleted: bool


class AgentProductionSummaryOut(SchemaBaseModel):
    id: UUID
    name: str
    status: AgentProductionStatus
    current_stage: str
    mode: AgentProductionMode
    source_type: str
    character_count: int
    style_id: Optional[UUID] = None
    generation_ratio: Optional[ProjectGenerationRatio] = None
    video_resolution: Optional[AgentVideoResolution] = None
    video_model_id: Optional[UUID] = None
    configuration_required: bool
    consumed_points: int
    error_summary: Optional[str] = None
    active_task_count: int = Field(ge=0)
    has_active_tasks: bool
    should_poll: bool
    next_poll_seconds: Optional[int] = Field(default=None, ge=1)
    created_at: datetime
    updated_at: datetime


class AgentProductionProjectListOut(SchemaBaseModel):
    items: List[AgentProductionSummaryOut]
    total: int
    page: int
    page_size: int


def _validate_initial_video_model_selection(
    mode: AgentProductionMode,
    video_model_id: Optional[UUID],
) -> None:
    if mode == "automatic" and video_model_id is None:
        raise ValueError("自动模式必须提前选择视频模型")
    if mode == "supervised" and video_model_id is not None:
        raise ValueError("审核模式应在生成视频时选择视频模型")


class AgentEpisodePlanItemOut(SchemaBaseModel):
    model_config = ConfigDict(extra="allow")

    plan_id: UUID
    episode_number: int
    title: str
    content: str
    source_content: str = Field(description="按原文字符范围切分出的本集完整剧本")
    logline: str = ""
    opening_hook: str
    goal: str
    conflict: str
    climax: str
    ending_hook: str
    source_start: Optional[int] = None
    source_end: Optional[int] = None
    source_block_ids: List[Any] = Field(default_factory=list)
    characters: List[Any] = Field(default_factory=list)
    character_variants: List[Any] = Field(default_factory=list)
    scenes: List[Any] = Field(default_factory=list)
    scene_variants: List[Any] = Field(default_factory=list)
    props: List[Any] = Field(default_factory=list)
    prop_variants: List[Any] = Field(default_factory=list)
    continuity_notes: List[str] = Field(default_factory=list)


class AgentEpisodePlanListOut(SchemaBaseModel):
    production_id: UUID
    step_id: UUID
    checkpoint_id: UUID
    version: int
    status: str
    planning_summary: str = ""
    items: List[AgentEpisodePlanItemOut]


class AgentEpisodePlanUpdateRequest(SchemaBaseModel):
    expected_version: int = Field(..., ge=1)
    title: Optional[str] = Field(default=None, min_length=1, max_length=128)
    content: Optional[str] = Field(default=None, min_length=1)
    logline: Optional[str] = None
    opening_hook: Optional[str] = Field(default=None, min_length=1)
    goal: Optional[str] = Field(default=None, min_length=1)
    conflict: Optional[str] = Field(default=None, min_length=1)
    climax: Optional[str] = Field(default=None, min_length=1)
    ending_hook: Optional[str] = Field(default=None, min_length=1)
    source_start: Optional[int] = Field(default=None, ge=0)
    source_end: Optional[int] = Field(default=None, ge=1)
    position: Optional[int] = Field(default=None, ge=1, le=200)
    characters: Optional[List[Any]] = None
    character_variants: Optional[List[Any]] = None
    scenes: Optional[List[Any]] = None
    scene_variants: Optional[List[Any]] = None
    props: Optional[List[Any]] = None
    prop_variants: Optional[List[Any]] = None
    continuity_notes: Optional[List[str]] = None

    @model_validator(mode="after")
    def require_update_field(self):
        if not self.model_dump(exclude={"expected_version"}, exclude_none=True):
            raise ValueError("至少提供一个需要更新的字段")
        if (self.source_start is None) != (self.source_end is None):
            raise ValueError("source_start 和 source_end 必须同时提供")
        if (
            self.source_start is not None
            and self.source_end is not None
            and self.source_end <= self.source_start
        ):
            raise ValueError("source_end 必须大于 source_start")
        return self


class AgentEpisodePlanMergeRequest(SchemaBaseModel):
    expected_version: int = Field(..., ge=1)
    plan_ids: List[UUID] = Field(..., min_length=2, max_length=2)
    title: Optional[str] = Field(default=None, min_length=1, max_length=128)
    content: Optional[str] = Field(default=None, min_length=1)

    @field_validator("plan_ids")
    @classmethod
    def require_distinct_plan_ids(cls, value: List[UUID]) -> List[UUID]:
        if len(set(value)) != 2:
            raise ValueError("合并操作需要两个不同的剧集规划")
        return value


class AgentEpisodePlanSplitRequest(SchemaBaseModel):
    expected_version: int = Field(..., ge=1)
    split_at: int = Field(..., ge=1)
    first_title: Optional[str] = Field(default=None, min_length=1, max_length=128)
    second_title: Optional[str] = Field(default=None, min_length=1, max_length=128)
    first_content: Optional[str] = Field(default=None, min_length=1)
    second_content: Optional[str] = Field(default=None, min_length=1)


class AgentEpisodePlanImpactOut(SchemaBaseModel):
    production_id: UUID
    version: int
    chapter_count: int
    existing_active_chapter_count: int
    will_create_count: int
    source_covered_characters: int
    source_character_count: int
    warnings: List[str]


class AgentEpisodePlanConfirmRequest(SchemaBaseModel):
    expected_version: int = Field(..., ge=1)
    idempotency_key: str = Field(..., min_length=8, max_length=128)

    @field_validator("idempotency_key")
    @classmethod
    def normalize_idempotency_key(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 8:
            raise ValueError("幂等键至少需要 8 个字符")
        return normalized


class AgentEpisodePlanConfirmOut(SchemaBaseModel):
    production_id: UUID
    version: int
    chapter_ids: List[UUID]
    created_count: int
    already_confirmed: bool
