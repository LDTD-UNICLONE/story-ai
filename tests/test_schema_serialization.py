from datetime import datetime, timezone
from typing import Any

from pydantic import field_serializer

from app.schemas.base import SchemaBaseModel


class SerializationContractSchema(SchemaBaseModel):
    created_at: datetime
    extra: dict[str, Any]
    label: str

    @field_serializer("label")
    def serialize_label(self, value: str) -> str:
        return value.upper()


def test_schema_base_uses_no_deprecated_json_encoders() -> None:
    assert "json_encoders" not in SchemaBaseModel.model_config


def test_beijing_datetime_json_contract_is_unchanged() -> None:
    value = SerializationContractSchema(
        created_at=datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=timezone.utc),
        extra={
            "nested_at": datetime(2026, 1, 2, 3, 4, 5, 678901),
            "items": [datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=timezone.utc)],
        },
        label="ready",
    )

    assert value.model_dump(mode="json") == {
        "created_at": "2026-01-02T11:04:05+08:00",
        "extra": {
            "nested_at": "2026-01-02T03:04:05.678901",
            "items": ["2026-01-02T03:04:05.678901Z"],
        },
        "label": "READY",
    }
    assert value.model_dump_json() == (
        '{"created_at":"2026-01-02T11:04:05+08:00",'
        '"extra":{"nested_at":"2026-01-02T03:04:05.678901",'
        '"items":["2026-01-02T03:04:05.678901Z"]},"label":"READY"}'
    )


def test_python_dump_keeps_datetime_values() -> None:
    created_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    nested_at = datetime(2026, 1, 2, 3, 4, 5)
    value = SerializationContractSchema(
        created_at=created_at,
        extra={"nested_at": nested_at},
        label="ready",
    )

    dumped = value.model_dump(mode="python")

    assert dumped["created_at"] is created_at
    assert dumped["extra"]["nested_at"] is nested_at
    assert dumped["label"] == "READY"
