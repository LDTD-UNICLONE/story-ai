from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from app.schemas.base import SchemaBaseModel


AgentWorkflowStepStatus = Literal[
    "not_started",
    "processing",
    "waiting_review",
    "completed",
    "failed",
    "invalidated",
]


class AgentWorkflowStepOut(SchemaBaseModel):
    id: UUID
    step_number: int
    step_code: str
    title: str
    status: AgentWorkflowStepStatus
    completed: bool
    can_view: bool
    is_current: bool
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    extra: Dict[str, Any]


class AgentEpisodeWorkflowOut(SchemaBaseModel):
    chapter_id: UUID
    episode_number: int
    title: str
    steps: List[AgentWorkflowStepOut]


class AgentWorkflowOut(SchemaBaseModel):
    production_id: UUID
    mode: Literal["supervised", "automatic"]
    current_step: int
    steps: List[AgentWorkflowStepOut]
    episodes: List[AgentEpisodeWorkflowOut]
