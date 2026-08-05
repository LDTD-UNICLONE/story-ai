from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, field_serializer

from app.core.timezone import to_beijing_datetime


def serialize_beijing_datetime(value: datetime) -> str:
    return to_beijing_datetime(value).isoformat(timespec="seconds")


class SchemaBaseModel(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    @field_serializer("*", check_fields=False, when_used="json")
    def serialize_datetime_field(self, value: Any) -> Any:
        if isinstance(value, datetime):
            return serialize_beijing_datetime(value)
        return value


def dump_beijing_json(value: Any) -> Any:
    if isinstance(value, datetime):
        return serialize_beijing_datetime(value)
    if isinstance(value, dict):
        return {key: dump_beijing_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [dump_beijing_json(item) for item in value]
    if isinstance(value, tuple):
        return [dump_beijing_json(item) for item in value]
    return value
