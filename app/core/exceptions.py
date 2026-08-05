import logging
from typing import Any, Optional

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.public_messages import sanitize_public_data, sanitize_public_message
from app.core.responses import error
from app.core.logging import log_extra

logger = logging.getLogger(__name__)


class AppException(Exception):
    def __init__(
        self,
        message: str,
        code: int = 40000,
        status_code: int = 400,
        data: Optional[Any] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.data = data


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppException)
    async def app_exception_handler(request: Request, exc: AppException) -> JSONResponse:
        log_level = logging.WARNING if exc.status_code >= 500 else logging.INFO
        logger.log(
            log_level,
            "Application exception: %s %s %s",
            request.method,
            request.url.path,
            exc.message,
            extra=log_extra(
                event="app_exception",
                method=request.method,
                path=request.url.path,
                status_code=exc.status_code,
                code=exc.code,
            ),
        )
        return error(
            message=sanitize_public_message(exc.message),
            code=exc.code,
            data=sanitize_public_data(exc.data),
            http_status=exc.status_code,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        message = exc.detail if isinstance(exc.detail, str) else "请求失败"
        logger.info(
            "HTTP exception: %s %s %s",
            request.method,
            request.url.path,
            message,
            extra=log_extra(
                event="http_exception",
                method=request.method,
                path=request.url.path,
                status_code=exc.status_code,
            ),
        )
        return error(
            message=sanitize_public_message(message, fallback="请求失败"),
            code=exc.status_code,
            http_status=exc.status_code,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        validation_errors = safe_validation_errors(exc)
        logger.info(
            "Validation error: %s %s errors=%s",
            request.method,
            request.url.path,
            validation_errors,
            extra=log_extra(
                event="validation_error",
                method=request.method,
                path=request.url.path,
                errors=validation_errors,
            ),
        )
        return error(
            message="参数校验失败",
            code=42200,
            data=validation_errors,
            http_status=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "Unhandled exception: %s %s",
            request.method,
            request.url.path,
            extra=log_extra(
                event="unhandled_exception", method=request.method, path=request.url.path
            ),
        )
        return error(message="服务器内部错误", code=50000, http_status=500)


def safe_validation_errors(exc: RequestValidationError) -> list[dict]:
    return [
        {
            "type": item.get("type"),
            "loc": item.get("loc"),
            "msg": item.get("msg"),
        }
        for item in exc.errors()
    ]
