from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field

from app.schemas.base import SchemaBaseModel
from app.schemas.upload import SeedanceImageOut


class ProjectMediaImport(SchemaBaseModel):
    model_config = ConfigDict(extra="forbid")

    source_type: Literal["seedance_image"]
    source_id: UUID


class ProjectMediaOut(SchemaBaseModel):
    id: UUID
    project_id: UUID
    media_type: Literal["image", "video"] = "image"
    source_type: str
    source_verified: bool
    source_id: UUID | None
    url: str
    filename: str
    created_at: datetime
    review: SeedanceImageOut | None


class ProjectMediaBatchRequest(SchemaBaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: list[UUID] = Field(min_length=1, max_length=100)
