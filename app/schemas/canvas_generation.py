from uuid import UUID
from typing import Literal

from pydantic import ConfigDict, Field, StrictBool

from app.schemas.base import SchemaBaseModel


CanvasTaskStatus = Literal["active", "pending", "running", "success", "failed", "all"]


class GenerationSchema(SchemaBaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class CanvasGenerationParameters(GenerationSchema):
    temperature: float | None = Field(default=None, ge=0, le=2, strict=True)
    max_tokens: int | None = Field(default=None, ge=1, le=65536, strict=True)
    aspect_ratio: str | None = Field(default=None, min_length=1, max_length=32)
    resolution: str | None = Field(default=None, min_length=1, max_length=32)
    size: str | None = Field(default=None, min_length=1, max_length=32)
    n: int | None = Field(default=None, ge=1, le=10, strict=True)
    duration: int | None = Field(default=None, ge=1, le=120, strict=True)
    seed: int | None = Field(default=None, ge=-1, le=2147483647, strict=True)
    generate_audio: StrictBool | None = None
    watermark: StrictBool | None = None


class CanvasGenerationSettings(GenerationSchema):
    ai_model_id: UUID
    parameters: CanvasGenerationParameters = Field(default_factory=CanvasGenerationParameters)


class CanvasGenerationSubmit(GenerationSchema):
    expected_content_revision: int = Field(ge=1, strict=True)
    idempotency_key: UUID


class CanvasGenerationSelect(GenerationSchema):
    expected_content_revision: int = Field(ge=1, strict=True)
    result_index: int = Field(default=0, ge=0, le=99, strict=True)


class CanvasGenerationBatchRequest(GenerationSchema):
    ids: list[UUID] = Field(min_length=1, max_length=100)
