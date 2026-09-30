from uuid import uuid4

import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from app.schemas.canvas_generation import CanvasGenerationParameters, CanvasGenerationSubmit
from app.schemas.project_canvas import CanvasContent, CanvasNodeInput


@pytest.mark.parametrize("field,value", [
    ("image_urls", ["https://example.com/image.png"]), ("messages", []),
    ("system_prompt", "hidden instructions"),
    ("_model_capabilities", {}), ("review_status", "ready"),
    ("duration", True), ("duration", 0), ("n", 0), ("n", 11),
    ("max_tokens", -1), ("temperature", float("inf")), ("temperature", True),
])
def test_parameters_reject_forged_inputs_and_invalid_values(field, value):
    with pytest.raises(ValidationError):
        CanvasGenerationParameters(**{field: value})


def test_content_settings_json_roundtrip_and_read_only_fields():
    content = CanvasContent(text="use @{input UUID}", generation={"ai_model_id": uuid4()})
    assert CanvasContent.model_validate_json(content.model_dump_json()) == content
    with pytest.raises(ValidationError):
        CanvasNodeInput(id=uuid4(), kind="image", selected_generation_id=uuid4())
    with pytest.raises(ValidationError):
        CanvasGenerationSubmit(expected_content_revision=True, idempotency_key=uuid4())
    with pytest.raises(ValidationError):
        CanvasContent(text="user prompt", system_prompt="hidden instructions")


def test_generation_routes_registered_in_public_api():
    from app.api.v1.router import api_router

    app = FastAPI()
    app.include_router(api_router)
    paths = app.openapi()["paths"]
    route = "/projects/{project_id}/canvases/{canvas_id}/nodes/{node_id}/generations"
    assert {"post", "get"} <= set(paths[route])
    assert "post" in paths[route + "/{generation_id}/select"]
