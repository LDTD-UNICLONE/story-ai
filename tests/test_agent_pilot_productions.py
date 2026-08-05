from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.main import app
from app.schemas.agent_pilot_production import AgentPilotActionRequest
from app.schemas.project_storyboard import ProjectStoryboardImageGenerateRequest


def test_agent_pilot_routes_are_registered() -> None:
    routes = {
        (path, method.upper())
        for path, methods in app.openapi()["paths"].items()
        for method in methods
    }
    prefix = "/api/v1/agent-productions/{production_id}/pilot"
    assert (prefix, "GET") in routes
    assert (f"{prefix}/storyboards", "POST") in routes
    assert (f"{prefix}/images", "POST") in routes
    assert (f"{prefix}/videos", "POST") in routes
    assert (f"{prefix}/confirm", "POST") in routes


def test_pilot_action_requires_lock_version_and_stable_idempotency_key() -> None:
    payload = AgentPilotActionRequest(
        expected_core_asset_lock_version=2,
        idempotency_key="  pilot-action-v2  ",
    )
    assert payload.idempotency_key == "pilot-action-v2"

    with pytest.raises(ValidationError):
        AgentPilotActionRequest(
            expected_core_asset_lock_version=0,
            idempotency_key="short",
        )


def test_storyboard_image_request_accepts_explicit_production_model() -> None:
    model_id = uuid4()
    payload = ProjectStoryboardImageGenerateRequest(ai_model_id=model_id)
    assert payload.ai_model_id == model_id
