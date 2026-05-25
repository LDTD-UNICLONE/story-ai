

from app.schemas.base import SchemaBaseModel


class UploadFileOut(SchemaBaseModel):
    url: str
    object_key: str
    filename: str
    content_type: str
    size: int
    file_type: str
