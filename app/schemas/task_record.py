from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import ConfigDict, Field, field_serializer

from app.core.public_messages import sanitize_public_data, sanitize_public_message
from app.schemas.base import SchemaBaseModel


class UserTaskRecordOut(SchemaBaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    ai_model_id: Optional[UUID] = None
    points_transaction_id: Optional[UUID] = None
    business_type: str
    business_id: Optional[UUID] = None
    generation_type: str
    status: str
    title: str
    prompt: str
    result: Optional[str] = None
    points_cost: int
    extra: Dict[str, Any]
    created_at: datetime

    @field_serializer("result")
    def serialize_result(self, value: Optional[str]) -> Optional[str]:
        return sanitize_public_message(value) if value is not None else None

    @field_serializer("extra")
    def serialize_extra(self, value: Dict[str, Any]) -> Dict[str, Any]:
        return sanitize_public_data(value)


class UserTaskRecordListOut(SchemaBaseModel):
    items: List[UserTaskRecordOut]
    total: int
    page: int
    page_size: int


class TaskRecordGenerationTypeOption(SchemaBaseModel):
    label: str
    value: str
    business_type: str


class TaskRecordOptionsOut(SchemaBaseModel):
    business_types: List[Dict[str, str]]
    generation_types: List[TaskRecordGenerationTypeOption]
    statuses: List[Dict[str, str]]


class AdminTaskRecordInterruptRequest(SchemaBaseModel):
    reason: Optional[str] = Field(default=None, max_length=200)
