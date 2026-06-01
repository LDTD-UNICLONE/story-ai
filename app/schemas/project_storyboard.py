from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from pydantic import ConfigDict, Field, field_serializer, model_validator

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
    scene_state: Optional[str] = None
    shot_size: Optional[str] = None
    camera_angle: Optional[str] = None
    camera_movement: Optional[str] = None
    screen_execution: Optional[str] = None
    characters: List[str]
    props: List[str]
    action: Optional[str] = None
    event_goal: Optional[str] = None
    character_action: Optional[str] = None
    character_expression: Optional[str] = None
    dialogue: Optional[str] = None
    sound_effect: Optional[str] = None
    atmosphere: Optional[str] = None
    image_prompt: Optional[str] = None
    video_prompt: Optional[str] = None
    duration_suggestion: Optional[str] = None
    production_focus: Optional[str] = None
    negative_prompt: Optional[str] = None
    ending_frame: Optional[str] = None
    split_reason: Optional[str] = None
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


class ProjectStoryboardRequestModel(SchemaBaseModel):
    model_config = ConfigDict(extra="forbid")


_IGNORED_STORYBOARD_UPDATE_FIELDS = {
    "id",
    "project_id",
    "chapter_id",
    "user_id",
    "ai_model_id",
    "is_enabled",
    "created_at",
    "updated_at",
    "scene_time",
    "emotion",
    "visual_description",
    "core_action",
    "storyboard_image_prompt",
    "production_focus_base",
    "negative_prompt_base",
}


class ProjectStoryboardUpdateRequest(ProjectStoryboardRequestModel):
    shot_number: Optional[int] = Field(default=None, ge=1)
    title: Optional[str] = Field(default=None, min_length=1, max_length=128)
    source_content: Optional[str] = Field(default=None, min_length=1)
    scene_name: Optional[str] = Field(default=None, max_length=128)
    scene_state: Optional[str] = Field(default=None, max_length=128)
    shot_size: Optional[str] = Field(default=None, max_length=64)
    camera_angle: Optional[str] = Field(default=None, max_length=128)
    camera_movement: Optional[str] = None
    screen_execution: Optional[str] = None
    characters: Optional[List[str]] = None
    props: Optional[List[str]] = None
    action: Optional[str] = None
    event_goal: Optional[str] = None
    character_action: Optional[str] = None
    character_expression: Optional[str] = None
    dialogue: Optional[str] = None
    sound_effect: Optional[str] = None
    atmosphere: Optional[str] = None
    image_prompt: Optional[str] = None
    video_prompt: Optional[str] = None
    duration_suggestion: Optional[str] = Field(default=None, max_length=64)
    production_focus: Optional[str] = None
    negative_prompt: Optional[str] = None
    ending_frame: Optional[str] = None
    split_reason: Optional[str] = None
    extra: Optional[Dict[str, Any]] = None

    @model_validator(mode="before")
    @classmethod
    def ignore_uneditable_or_removed_fields(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        return {
            key: item
            for key, item in value.items()
            if key not in _IGNORED_STORYBOARD_UPDATE_FIELDS
        }


class ProjectStoryboardCreateRequest(ProjectStoryboardRequestModel):
    insert_after_storyboard_id: Optional[UUID] = None
    shot_number: Optional[int] = Field(default=None, ge=1)
    title: str = Field(default="新分镜", min_length=1, max_length=128)
    source_content: str = ""
    scene_name: Optional[str] = Field(default=None, max_length=128)
    scene_state: Optional[str] = Field(default=None, max_length=128)
    shot_size: Optional[str] = Field(default=None, max_length=64)
    camera_angle: Optional[str] = Field(default=None, max_length=128)
    camera_movement: Optional[str] = None
    screen_execution: Optional[str] = None
    characters: List[str] = Field(default_factory=list)
    props: List[str] = Field(default_factory=list)
    action: Optional[str] = None
    event_goal: Optional[str] = None
    character_action: Optional[str] = None
    character_expression: Optional[str] = None
    dialogue: Optional[str] = None
    sound_effect: Optional[str] = None
    atmosphere: Optional[str] = None
    image_prompt: Optional[str] = None
    video_prompt: Optional[str] = None
    duration_suggestion: Optional[str] = Field(default=None, max_length=64)
    production_focus: Optional[str] = None
    negative_prompt: Optional[str] = None
    ending_frame: Optional[str] = None
    split_reason: Optional[str] = None
    extra: Optional[Dict[str, Any]] = None


class ProjectStoryboardAnalyzeRequest(ProjectStoryboardRequestModel):
    ai_model_id: UUID
    analysis_prompt: Optional[str] = Field(default=None, min_length=1)
    extra: Optional[Dict[str, Any]] = None


class ProjectStoryboardRefineRequest(ProjectStoryboardRequestModel):
    ai_model_id: UUID
    extra: Optional[Dict[str, Any]] = None


class ProjectStoryboardPromptRequest(ProjectStoryboardRequestModel):
    ai_model_id: UUID
    extra: Optional[Dict[str, Any]] = None


class ProjectStoryboardAnalyzeOut(SchemaBaseModel):
    task_record_id: UUID
    status: str
    points_cost: int


class ProjectStoryboardUnitRequest(ProjectStoryboardRequestModel):
    title: str = Field(min_length=1, max_length=128)
    source_content: str = Field(min_length=1)
    event_goal: Optional[str] = None
    scene_name: Optional[str] = Field(default=None, max_length=128)
    characters: List[str] = Field(default_factory=list)
    props: List[str] = Field(default_factory=list)
    action: str = Field(min_length=1)
    dialogue: str = ""
    split_reason: Optional[str] = None
    extra: Optional[Dict[str, Any]] = None


class ProjectStoryboardMergeRequest(ProjectStoryboardRequestModel):
    storyboard_ids: List[UUID] = Field(min_length=2)
    title: Optional[str] = Field(default=None, min_length=1, max_length=128)
    source_content: Optional[str] = Field(default=None, min_length=1)
    event_goal: Optional[str] = None
    scene_name: Optional[str] = Field(default=None, max_length=128)
    characters: Optional[List[str]] = None
    props: Optional[List[str]] = None
    action: Optional[str] = None
    dialogue: Optional[str] = None
    split_reason: Optional[str] = None
    extra: Optional[Dict[str, Any]] = None


class ProjectStoryboardSplitRequest(ProjectStoryboardRequestModel):
    units: List[ProjectStoryboardUnitRequest] = Field(min_length=2)


class ProjectStoryboardVideoGenerateRequest(ProjectStoryboardRequestModel):
    ai_model_id: UUID
    generation_mode: Literal["reference", "first_last_frame", "storyboard"] = "reference"
    resolution: Literal["480p", "720p", "1080p"] = "720p"
    return_last_frame: bool = False
    prompt: Optional[str] = Field(default=None, min_length=1)
    character_ids: List[UUID] = Field(default_factory=list)
    scene_ids: List[UUID] = Field(default_factory=list)
    prop_ids: List[UUID] = Field(default_factory=list)
    uploaded_images: List[str] = Field(default_factory=list)
    first_frame_url: Optional[str] = Field(default=None, max_length=2048)
    last_frame_url: Optional[str] = Field(default=None, max_length=2048)
    extra: Optional[Dict[str, Any]] = None

    @model_validator(mode="before")
    @classmethod
    def normalize_media_url_fields(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        for key in ("first_frame_url", "last_frame_url"):
            data[key] = _extract_request_media_url(data.get(key)) or data.get(key)
        if "uploaded_images" in data:
            data["uploaded_images"] = [
                url
                for url in (_extract_request_media_url(item) for item in _as_request_list(data.get("uploaded_images")))
                if url
            ]
        return data


def _as_request_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _extract_request_media_url(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("url", "image_url", "file_url", "oss_url"):
            nested = value.get(key)
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
            if isinstance(nested, dict) and isinstance(nested.get("url"), str):
                return nested["url"].strip()
    return ""


class ProjectStoryboardVideoGenerateOut(SchemaBaseModel):
    task_record_id: UUID
    storyboard_id: UUID
    generation_mode: str
    resolution: str
    return_last_frame: bool
    status: str
    points_cost: int
