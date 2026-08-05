from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.schemas.base import SchemaBaseModel
from app.schemas.agent_production import AgentProductionDetailOut


AgentJobStage = Literal["storyboard", "image", "video"]


class AgentProductionStageOut(SchemaBaseModel):
    stage: AgentJobStage
    status: str
    task_record_id: Optional[UUID] = None
    task_status: Optional[str] = None
    attempt_count: int
    points_cost: int
    error_message: Optional[str] = None
    can_retry: bool
    requires_retry_confirmation: bool
    can_skip: bool
    manually_resolved: bool
    replacement_url: Optional[str] = None


class AgentProductionStoryboardMatrixOut(SchemaBaseModel):
    storyboard_id: UUID
    shot_number: int
    title: str
    image: AgentProductionStageOut
    video: AgentProductionStageOut


class AgentProductionEpisodeMatrixOut(SchemaBaseModel):
    chapter_id: UUID
    episode_number: int
    title: str
    is_pilot: bool
    storyboard: AgentProductionStageOut
    storyboards: List[AgentProductionStoryboardMatrixOut]


class AgentProductionMatrixOut(SchemaBaseModel):
    production_id: UUID
    status: str
    current_stage: str
    total_episode_count: int
    completed_episode_count: int
    failed_item_count: int
    active_task_count: int
    episodes: List[AgentProductionEpisodeMatrixOut]


class AgentProductionExceptionOut(SchemaBaseModel):
    exception_key: str
    category: str
    stage: AgentJobStage
    scope_type: Literal["chapter", "storyboard"]
    scope_id: UUID
    chapter_id: UUID
    storyboard_id: Optional[UUID] = None
    task_record_id: Optional[UUID] = None
    message: str
    attempt_count: int
    can_retry: bool
    requires_retry_confirmation: bool
    can_skip: bool


class AgentProductionExceptionListOut(SchemaBaseModel):
    items: List[AgentProductionExceptionOut]
    total: int
    page: int
    page_size: int


class AgentProductionEventOut(SchemaBaseModel):
    id: UUID
    step_id: Optional[UUID] = None
    actor_user_id: Optional[UUID] = None
    event_type: str
    source: str
    payload: Dict[str, Any]
    created_at: datetime


class AgentProductionEventListOut(SchemaBaseModel):
    items: List[AgentProductionEventOut]
    total: int
    page: int
    page_size: int


class AgentProductionCostStageOut(SchemaBaseModel):
    stage: str
    task_count: int
    charged_points: int
    refunded_points: int
    net_points: int


class AgentProductionCostOut(SchemaBaseModel):
    production_id: UUID
    charged_points: int
    refunded_points: int
    net_points: int
    production_consumed_points: int
    task_count: int
    stages: List[AgentProductionCostStageOut]


class AgentControllerStateOut(SchemaBaseModel):
    status: str
    lease_expires_at: Optional[datetime] = None
    attempt_count: int
    last_started_at: Optional[datetime] = None
    last_finished_at: Optional[datetime] = None
    last_error: Optional[str] = None
    last_result: Dict[str, Any]


class AgentProductionWorkbenchOut(SchemaBaseModel):
    production: AgentProductionDetailOut
    matrix: AgentProductionMatrixOut
    costs: AgentProductionCostOut
    controller: Optional[AgentControllerStateOut] = None
    next_action: Optional[str] = None
    available_actions: List[str]


class AgentJobRetryRequest(SchemaBaseModel):
    expected_core_asset_lock_version: int = Field(ge=1)
    stage: AgentJobStage
    scope_ids: List[UUID] = Field(min_length=1, max_length=50)
    idempotency_key: str = Field(min_length=8, max_length=128)
    confirm_over_retry_limit: bool = False

    @field_validator("scope_ids")
    @classmethod
    def unique_scope_ids(cls, value: List[UUID]) -> List[UUID]:
        return list(dict.fromkeys(value))

    @field_validator("idempotency_key")
    @classmethod
    def normalize_idempotency_key(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 8:
            raise ValueError("幂等键至少需要 8 个字符")
        return normalized


class AgentJobSkipRequest(SchemaBaseModel):
    expected_core_asset_lock_version: int = Field(ge=1)
    stage: AgentJobStage
    scope_ids: List[UUID] = Field(min_length=1, max_length=50)
    reason: str = Field(min_length=2, max_length=500)
    replacement_url: Optional[str] = Field(default=None, max_length=1024)
    idempotency_key: str = Field(min_length=8, max_length=128)

    @field_validator("scope_ids")
    @classmethod
    def unique_scope_ids(cls, value: List[UUID]) -> List[UUID]:
        return list(dict.fromkeys(value))

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 2:
            raise ValueError("人工处理原因至少需要 2 个字符")
        return normalized

    @field_validator("idempotency_key")
    @classmethod
    def normalize_idempotency_key(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 8:
            raise ValueError("幂等键至少需要 8 个字符")
        return normalized

    @field_validator("replacement_url")
    @classmethod
    def validate_replacement_url(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized.startswith(("http://", "https://")):
            raise ValueError("人工替换地址必须是 http 或 https URL")
        return normalized

    @model_validator(mode="after")
    def validate_replacement_scope(self):
        if self.replacement_url and self.stage == "storyboard":
            raise ValueError("分镜分析阶段不支持媒体替换地址")
        if self.replacement_url and len(self.scope_ids) != 1:
            raise ValueError("一次人工替换只能处理一个镜头")
        return self


class AgentJobActionOut(SchemaBaseModel):
    action: Literal["retry", "skip"]
    stage: AgentJobStage
    requested_count: int
    affected_count: int
    task_record_ids: List[UUID]
    affected_scope_ids: List[UUID]
    points_cost: int
    idempotent: bool
