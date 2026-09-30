
from typing import Any, Dict, Literal, Optional
from uuid import UUID

from pydantic import ConfigDict, StrictBool

from app.schemas.base import SchemaBaseModel


class UploadFileOut(SchemaBaseModel):
    url: str
    object_key: str
    filename: str
    content_type: str
    size: int
    file_type: str
    media_info: Optional[Dict[str, Any]] = None


class SeedanceImageOut(UploadFileOut):
    image_id: UUID
    review_status: Literal["pending", "processing", "ready", "failed", "uncertain"]
    can_reference: bool
    can_retry: bool
    retry_requires_confirmation: bool = False
    review_error: Optional[str] = None
    reused: bool = False


class SeedanceImageRetryRequest(SchemaBaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm_resubmit: StrictBool = False
