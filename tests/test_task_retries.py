from unittest.mock import AsyncMock, Mock

import pytest
from celery.exceptions import Retry

from app.core.config import settings
from app.core.exceptions import AppException
from app.tasks import (
    model_generation,
    project_asset_analysis,
    project_asset_generation,
    project_chapter,
    project_storyboard,
    project_storyboard_image,
    project_storyboard_video,
)


TASK_ID = "00000000-0000-0000-0000-000000000001"
RESOURCE_ID = "00000000-0000-0000-0000-000000000002"


@pytest.mark.parametrize(
    ("module", "task_name", "failure_name", "task_args"),
    [
        pytest.param(
            model_generation, "run_conversation_generation", "_fail_generation",
            (TASK_ID, RESOURCE_ID), id="conversation",
        ),
        pytest.param(
            project_chapter, "run_project_chapter_processing", "_fail_processing",
            (TASK_ID, RESOURCE_ID), id="chapter",
        ),
        pytest.param(
            project_asset_analysis, "run_project_asset_analysis", "_fail_analysis",
            (TASK_ID, RESOURCE_ID, "character"), id="asset-analysis",
        ),
        pytest.param(
            project_asset_generation, "run_project_asset_image_generation", "_fail_generation",
            (TASK_ID, "character", RESOURCE_ID), id="asset-image",
        ),
        pytest.param(
            project_storyboard, "run_project_storyboard_analysis", "_fail_analysis",
            (TASK_ID, RESOURCE_ID), id="storyboard-analysis",
        ),
        pytest.param(
            project_storyboard, "run_project_storyboard_stage", "_fail_analysis",
            (TASK_ID, RESOURCE_ID), id="storyboard-stage",
        ),
        pytest.param(
            project_storyboard_image, "run_project_storyboard_image_generation", "_fail_generation",
            (TASK_ID, RESOURCE_ID), id="storyboard-image",
        ),
        pytest.param(
            project_storyboard_video, "run_project_storyboard_video_generation", "_fail_generation",
            (TASK_ID, RESOURCE_ID), id="storyboard-video",
        ),
    ],
)
@pytest.mark.parametrize(
    ("error", "retries", "expected_delay", "expected_reason"),
    [
        pytest.param(AppException("busy", status_code=503), 0, 10, None, id="first-retry"),
        pytest.param(AppException("busy", status_code=503), 1, 20, None, id="backoff"),
        pytest.param(AppException("busy", status_code=503), 2, 25, None, id="backoff-cap"),
        pytest.param(
            AppException("busy", status_code=503), 3, None,
            "模型服务繁忙，已自动重试多次仍未成功，请稍后再试", id="retry-limit",
        ),
        pytest.param(
            AppException("busy", code=50202, status_code=429), 0, 10, None,
            id="retryable-provider-code-50202",
        ),
        pytest.param(
            AppException("busy", code=50206, status_code=408), 0, 10, None,
            id="retryable-provider-code-50206",
        ),
        pytest.param(
            AppException("请求无效", code=50202, status_code=400), 0, None, "请求无效",
            id="bad-request-overrides-provider-code",
        ),
        pytest.param(
            AppException("认证失败", code=50202, status_code=401), 0, None, "认证失败",
            id="unauthorized-overrides-provider-code",
        ),
        pytest.param(
            AppException("无访问权限", code=50206, status_code=403), 0, None, "无访问权限",
            id="forbidden-overrides-provider-code",
        ),
        pytest.param(
            AppException("结果无效", code=50231, status_code=502), 0, None, "结果无效",
            id="terminal-result-error",
        ),
        pytest.param(
            AppException("参数不支持", status_code=422), 0, None, "参数不支持",
            id="other-client-error",
        ),
        pytest.param(
            AppException("BadRequest", status_code=502), 0, None, "模型响应失败，请稍后再试",
            id="non-retryable-message-in-server-error",
        ),
        pytest.param(
            AppException("CONTENT POLICY", status_code=500), 0, None, "CONTENT POLICY",
            id="case-insensitive-policy-rejection",
        ),
        pytest.param(
            RuntimeError("APIMart 连接失败"), 0, None, "模型服务 连接失败",
            id="unexpected-error-is-sanitized",
        ),
        pytest.param(RuntimeError(), 0, None, "任务执行失败", id="empty-error"),
    ],
)
def test_generation_task_retry_behavior(
    monkeypatch, module, task_name, failure_name, task_args,
    error, retries, expected_delay, expected_reason,
) -> None:
    execution = AsyncMock(side_effect=error)
    failure = AsyncMock()
    retry = Mock(side_effect=Retry())
    task = getattr(module, task_name)
    monkeypatch.setattr(module, f"_{task_name}", execution)
    monkeypatch.setattr(module, failure_name, failure)
    monkeypatch.setattr(task, "retry", retry)
    monkeypatch.setattr(settings, "celery_task_max_retries", 3)
    monkeypatch.setattr(settings, "celery_task_retry_countdown_seconds", 10)
    monkeypatch.setattr(settings, "celery_task_retry_backoff_max_seconds", 25)

    task.push_request(retries=retries)
    try:
        if expected_delay is not None:
            with pytest.raises(Retry):
                task.run(*task_args)
            retry.assert_called_once_with(exc=error, countdown=expected_delay)
            failure.assert_not_awaited()
        else:
            task.run(*task_args)
            retry.assert_not_called()
            failure.assert_awaited_once()
            if module is project_chapter and retries == 3:
                expected_reason = "模型服务繁忙，请稍后再试"
            assert failure.await_args.args[-1] == expected_reason
            assert failure.await_args.kwargs["raw_reason"] == (str(error) or "任务执行失败")
        execution.assert_awaited_once()
    finally:
        task.pop_request()
