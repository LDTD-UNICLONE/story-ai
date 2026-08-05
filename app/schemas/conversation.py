from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from pydantic import ConfigDict, Field, field_serializer, field_validator, model_validator

from app.core.public_messages import sanitize_public_data, sanitize_public_message
from app.schemas.base import SchemaBaseModel


class ConversationOut(SchemaBaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    title: str
    conversation_type: str
    ai_model_id: UUID
    is_enabled: bool
    created_at: datetime
    updated_at: datetime


class ConversationListOut(SchemaBaseModel):
    items: List[ConversationOut]
    total: int
    page: int
    page_size: int


class ConversationCreateRequest(SchemaBaseModel):
    title: str = Field(..., min_length=1, max_length=128)
    ai_model_id: UUID
    conversation_type: Literal["text", "image", "video"] = "text"


class ConversationUpdateRequest(SchemaBaseModel):
    title: str = Field(..., min_length=1, max_length=128)


class ConversationMessageOut(SchemaBaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    conversation_id: UUID
    user_id: UUID
    role: str
    content: str
    message_type: str
    extra: Dict[str, Any]
    ai_model_id: UUID
    turn_id: Optional[UUID] = None
    sequence_no: Optional[int] = None
    status: Optional[str] = None
    client_message_id: Optional[str] = None
    created_at: datetime

    @field_serializer("content")
    def serialize_content(self, value: str) -> str:
        return sanitize_public_message(value, fallback=value)

    @field_serializer("extra")
    def serialize_extra(self, value: Dict[str, Any]) -> Dict[str, Any]:
        return sanitize_public_data(value)


class ConversationMessageListOut(SchemaBaseModel):
    items: List[ConversationMessageOut]
    total: int
    page: int
    page_size: int


_MESSAGE_EXTRA_COMPAT_FIELDS = {
    "aspect_ratio",
    "audio",
    "audio_url",
    "audio_urls",
    "audioUrl",
    "audioUrls",
    "audios",
    "attachment",
    "attachment_url",
    "attachment_urls",
    "attachmentUrl",
    "attachmentUrls",
    "attachments",
    "duration",
    "duration_seconds",
    "file",
    "file_list",
    "file_url",
    "file_urls",
    "fileList",
    "fileUrl",
    "fileUrls",
    "files",
    "first_frame",
    "first_frame_url",
    "firstFrame",
    "firstFrameUrl",
    "generation_mode",
    "generate_audio",
    "image",
    "image_url",
    "image_urls",
    "imageUrl",
    "imageUrls",
    "images",
    "last_frame",
    "last_frame_url",
    "lastFrame",
    "lastFrameUrl",
    "media",
    "media_items",
    "ratio",
    "reference_audio",
    "reference_audio_url",
    "reference_audios",
    "reference_audio_urls",
    "reference_image",
    "reference_image_url",
    "reference_images",
    "reference_image_urls",
    "reference_video",
    "reference_video_url",
    "reference_videos",
    "reference_video_urls",
    "referenceAudio",
    "referenceAudioUrl",
    "referenceAudios",
    "referenceAudioUrls",
    "referenceImage",
    "referenceImageUrl",
    "referenceImageUrls",
    "referenceImages",
    "referenceVideo",
    "referenceVideoUrl",
    "referenceVideoUrls",
    "referenceVideos",
    "resolution",
    "return_last_frame",
    "seed",
    "uploaded_audios",
    "uploaded_file",
    "uploaded_files",
    "uploaded_images",
    "uploaded_videos",
    "uploadedAudios",
    "uploadedFile",
    "uploadedFiles",
    "uploadedImages",
    "uploadedVideos",
    "upload",
    "upload_file",
    "upload_files",
    "upload_list",
    "uploadFile",
    "uploadFiles",
    "uploadList",
    "uploads",
    "video",
    "video_url",
    "video_urls",
    "videoUrl",
    "videoUrls",
    "videos",
    "watermark",
}


class ConversationSendMessageRequest(SchemaBaseModel):
    content: str = Field(..., min_length=1)
    ai_model_id: Optional[UUID] = None
    client_message_id: Optional[str] = Field(default=None, min_length=1, max_length=128)
    extra: Optional[Dict[str, Any]] = None

    @field_validator("client_message_id")
    @classmethod
    def normalize_client_message_id(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("client_message_id 不能为空")
        return normalized

    @model_validator(mode="before")
    @classmethod
    def merge_top_level_extra_fields(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        extra = data.get("extra")
        normalized_extra: Dict[str, Any] = dict(extra) if isinstance(extra, dict) else {}
        for key in list(data.keys()):
            if key not in _MESSAGE_EXTRA_COMPAT_FIELDS:
                continue
            normalized_extra.setdefault(key, data.pop(key))
        if normalized_extra:
            data["extra"] = normalized_extra
        return data


class ConversationSendMessageOut(SchemaBaseModel):
    user_message: ConversationMessageOut
    assistant_message: ConversationMessageOut
    points_cost: int
    task_record_id: Optional[UUID] = None
    task_status: str = "pending"


class ConversationGenerationTaskOut(SchemaBaseModel):
    task_record_id: UUID
    conversation_id: UUID
    assistant_message_id: Optional[UUID] = None
    status: str
    task_status: str
    content: Optional[str] = None
    result: Optional[str] = None
    message: Optional[str] = None
    failed_reason: Optional[str] = None
    extra: Dict[str, Any]
    assistant_message: Optional[ConversationMessageOut] = None
    stop_polling: bool = False
    next_poll_seconds: Optional[int] = None
    created_at: datetime
    updated_at: datetime

    @field_serializer("content", "result", "message", "failed_reason")
    def serialize_optional_text(self, value: Optional[str]) -> Optional[str]:
        return sanitize_public_message(value) if value is not None else None

    @field_serializer("extra")
    def serialize_generation_extra(self, value: Dict[str, Any]) -> Dict[str, Any]:
        return sanitize_public_data(value)


class ConversationTaskStatusOut(SchemaBaseModel):
    task_id: str
    task_status: Optional[str] = None
    content: str
    extra: Dict[str, Any]

    @field_serializer("content")
    def serialize_task_content(self, value: str) -> str:
        return sanitize_public_message(value, fallback=value)

    @field_serializer("extra")
    def serialize_task_extra(self, value: Dict[str, Any]) -> Dict[str, Any]:
        return sanitize_public_data(value)
