from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from pydantic import ConfigDict, Field, field_serializer

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


class ConversationSendMessageRequest(SchemaBaseModel):
    content: str = Field(..., min_length=1)
    ai_model_id: Optional[UUID] = None
    extra: Optional[Dict[str, Any]] = None


class ConversationSendMessageOut(SchemaBaseModel):
    user_message: ConversationMessageOut
    assistant_message: ConversationMessageOut
    points_cost: int


class ConversationGenerationTaskOut(SchemaBaseModel):
    task_record_id: UUID
    conversation_id: UUID
    assistant_message_id: Optional[UUID] = None
    status: str
    task_status: str
    content: Optional[str] = None
    result: Optional[str] = None
    extra: Dict[str, Any]
    assistant_message: Optional[ConversationMessageOut] = None
    created_at: datetime
    updated_at: datetime

    @field_serializer("content", "result")
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
