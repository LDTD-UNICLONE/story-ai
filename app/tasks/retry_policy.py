from app.core.config import settings
from app.core.exceptions import AppException
from app.core.public_messages import sanitize_public_message


def is_retryable_provider_error(exc: Exception) -> bool:
    if isinstance(exc, AppException):
        if exc.code == 50231 or exc.status_code in {400, 401, 403}:
            return False
        if _is_non_retryable_provider_error_text(str(exc)):
            return False
        return exc.status_code >= 500 or exc.code in {50202, 50206}
    return False


def _is_non_retryable_provider_error_text(message: str) -> bool:
    normalized = message.lower()
    non_retryable_tokens = (
        "http 400",
        "badrequest",
        "invalidparameter",
        "sensitivecontentdetected",
        "privacyinformation",
        "real person",
        "not valid",
        "content policy",
    )
    return any(token in normalized for token in non_retryable_tokens)


def retry_countdown(retries: int) -> int:
    countdown = settings.celery_task_retry_countdown_seconds * (2**retries)
    return min(countdown, settings.celery_task_retry_backoff_max_seconds)


def user_failed_reason(
    exc: Exception,
    *,
    retryable_message: str = "模型服务繁忙，已自动重试多次仍未成功，请稍后再试",
) -> str:
    if is_retryable_provider_error(exc):
        return retryable_message
    return sanitize_public_message(str(exc) or "任务执行失败")
