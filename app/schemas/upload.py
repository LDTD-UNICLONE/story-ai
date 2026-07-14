
from typing import Any, Dict, Optional

from app.schemas.base import SchemaBaseModel


class UploadFileOut(SchemaBaseModel):
    url: str
    object_key: str
    filename: str
    content_type: str
    size: int
    file_type: str
    media_info: Optional[Dict[str, Any]] = None
