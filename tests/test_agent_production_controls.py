import pytest
from pydantic import ValidationError

from app.main import app
from app.schemas.agent_production_control import AgentJobRetryRequest, AgentJobSkipRequest


def test_agent_production_control_routes_are_registered() -> None:
    routes = {
        (path, method.upper())
        for path, methods in app.openapi()["paths"].items()
        for method in methods
    }
    prefix = "/api/v1/agent-productions/{production_id}"
    for suffix in ("matrix", "exceptions", "events", "costs"):
        assert (f"{prefix}/{suffix}", "GET") in routes
    assert (f"{prefix}/jobs/retry", "POST") in routes
    assert (f"{prefix}/jobs/skip", "POST") in routes


def test_retry_request_deduplicates_scope_ids() -> None:
    from uuid import uuid4

    scope_id = uuid4()
    payload = AgentJobRetryRequest(
        expected_core_asset_lock_version=1,
        stage="video",
        scope_ids=[scope_id, scope_id],
        idempotency_key="retry-video-v1",
        confirm_over_retry_limit=True,
    )
    assert payload.scope_ids == [scope_id]


def test_skip_replacement_requires_one_media_scope() -> None:
    from uuid import uuid4

    with pytest.raises(ValidationError):
        AgentJobSkipRequest(
            expected_core_asset_lock_version=1,
            stage="storyboard",
            scope_ids=[uuid4()],
            reason="人工处理",
            replacement_url="https://example.com/video.mp4",
            idempotency_key="skip-storyboard-v1",
        )

    with pytest.raises(ValidationError):
        AgentJobSkipRequest(
            expected_core_asset_lock_version=1,
            stage="video",
            scope_ids=[uuid4(), uuid4()],
            reason="人工替换",
            replacement_url="https://example.com/video.mp4",
            idempotency_key="skip-video-v1",
        )

    with pytest.raises(ValidationError):
        AgentJobSkipRequest(
            expected_core_asset_lock_version=1,
            stage="video",
            scope_ids=[uuid4()],
            reason="  ",
            idempotency_key="        ",
        )
