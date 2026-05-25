from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from pydantic import ConfigDict, Field, field_serializer

from app.core.public_messages import sanitize_public_data
from app.schemas.base import SchemaBaseModel


class ProjectStoryboardOut(SchemaBaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    chapter_id: UUID
    user_id: UUID
    ai_model_id: Optional[UUID] = None
    shot_number: int
    title: str
    source_content: str
    scene_name: Optional[str] = None
    scene_time: Optional[str] = None
    shot_size: Optional[str] = None
    camera_angle: Optional[str] = None
    camera_movement: Optional[str] = None
    screen_execution: Optional[str] = None
    characters: List[str]
    props: List[str]
    action: Optional[str] = None
    character_action: Optional[str] = None
    character_expression: Optional[str] = None
    dialogue: Optional[str] = None
    sound_effect: Optional[str] = None
    emotion: Optional[str] = None
    visual_description: Optional[str] = None
    image_prompt: Optional[str] = None
    video_prompt: Optional[str] = None
    duration_suggestion: Optional[str] = None
    production_focus: Optional[str] = None
    negative_prompt: Optional[str] = None
    extra: Dict[str, Any]
    is_enabled: bool
    created_at: datetime
    updated_at: datetime

    @field_serializer("extra")
    def serialize_extra(self, value: Dict[str, Any]) -> Dict[str, Any]:
        return sanitize_public_data(value)


class ProjectStoryboardListOut(SchemaBaseModel):
    items: List[ProjectStoryboardOut]
    total: int
    page: int
    page_size: int


class ProjectStoryboardUpdateRequest(SchemaBaseModel):
    shot_number: Optional[int] = Field(default=None, ge=1)
    title: Optional[str] = Field(default=None, min_length=1, max_length=128)
    source_content: Optional[str] = Field(default=None, min_length=1)
    scene_name: Optional[str] = Field(default=None, max_length=128)
    scene_time: Optional[str] = Field(default=None, max_length=64)
    shot_size: Optional[str] = Field(default=None, max_length=64)
    camera_angle: Optional[str] = Field(default=None, max_length=128)
    camera_movement: Optional[str] = None
    screen_execution: Optional[str] = None
    characters: Optional[List[str]] = None
    props: Optional[List[str]] = None
    action: Optional[str] = None
    character_action: Optional[str] = None
    character_expression: Optional[str] = None
    dialogue: Optional[str] = None
    sound_effect: Optional[str] = None
    emotion: Optional[str] = None
    visual_description: Optional[str] = None
    image_prompt: Optional[str] = None
    video_prompt: Optional[str] = None
    duration_suggestion: Optional[str] = Field(default=None, max_length=64)
    production_focus: Optional[str] = None
    negative_prompt: Optional[str] = None
    extra: Optional[Dict[str, Any]] = None


class ProjectStoryboardAnalyzeRequest(SchemaBaseModel):
    ai_model_id: UUID
    analysis_prompt: Optional[str] = Field(default=None, min_length=1)
    extra: Optional[Dict[str, Any]] = None


class ProjectStoryboardAnalyzeOut(SchemaBaseModel):
    task_record_id: UUID
    status: str
    points_cost: int


class ProjectStoryboardVideoGenerateRequest(SchemaBaseModel):
    ai_model_id: UUID
    generation_mode: Literal["reference", "first_last_frame", "storyboard"] = "reference"
    prompt: Optional[str] = Field(default=None, min_length=1)
    character_ids: List[UUID] = Field(default_factory=list)
    scene_ids: List[UUID] = Field(default_factory=list)
    prop_ids: List[UUID] = Field(default_factory=list)
    uploaded_images: List[str] = Field(default_factory=list)
    first_frame_url: Optional[str] = Field(default=None, max_length=512)
    last_frame_url: Optional[str] = Field(default=None, max_length=512)
    extra: Optional[Dict[str, Any]] = None


class ProjectStoryboardVideoGenerateOut(SchemaBaseModel):
    task_record_id: UUID
    storyboard_id: UUID
    generation_mode: str
    status: str
    points_cost: int
