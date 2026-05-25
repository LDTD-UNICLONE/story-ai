from typing import Any

from fastapi.responses import JSONResponse
from pydantic import ConfigDict

from app.core.timezone import now_beijing
from app.schemas.base import SchemaBaseModel, dump_beijing_json


class ApiResponse(SchemaBaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    code: int = 0
    message: str = "success"
    data: Any = None
    timestamp: str


def success(data: Any = None, message: str = "success", code: int = 0) -> ApiResponse:
    return ApiResponse(code=code, message=message, data=dump_beijing_json(data), timestamp=now_beijing())


def error(
    message: str = "error",
    code: int = 40000,
    data: Any = None,
    http_status: int = 400,
) -> JSONResponse:
    payload = ApiResponse(code=code, message=message, data=dump_beijing_json(data), timestamp=now_beijing())
    return JSONResponse(status_code=http_status, content=payload.model_dump(mode="json"))
