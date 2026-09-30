from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.project import ProjectCreateRequest, ProjectUpdateRequest, ProjectOut
from app.schemas.project_canvas import CanvasLayoutPatch, CanvasNodeLayoutPatch
from app.services.generation.media import extract_result_urls


@pytest.mark.parametrize("schema", [ProjectCreateRequest, ProjectUpdateRequest])
@pytest.mark.parametrize("field,value", [("style_id", uuid4()), ("generation_ratio", "16:9"), ("style", {})])
def test_project_settings_are_not_silently_accepted(schema, field, value):
    with pytest.raises(ValidationError):
        schema(name="Canvas", **{field: value})
    assert not {"style_id", "style", "generation_ratio"} & ProjectOut.model_fields.keys()


@pytest.mark.parametrize("value", [
    {}, {"x": None}, {"x": float("inf")}, {"x": 1_000_001},
    {"width": 0}, {"height": 10001}, {"content": {"text": "overwrite"}},
    {"media_id": uuid4()}, {"parent_id": uuid4()}, {"kind": "text"},
])
def test_layout_rejects_invalid_geometry_and_content_changes(value):
    with pytest.raises(ValidationError):
        CanvasNodeLayoutPatch(id=uuid4(), **value)


def test_layout_rejects_empty_duplicate_and_oversized_batches():
    entry = {"id": uuid4(), "x": 0}
    for data in ({}, {"viewport": None}, {"nodes": [entry, entry]}, {"nodes": [entry] * 501}):
        with pytest.raises(ValidationError):
            CanvasLayoutPatch(expected_revision=1, **data)
    change = CanvasNodeLayoutPatch(id=uuid4(), x=0)
    assert change.model_dump(exclude_unset=True).keys() == {"id", "x"}


@pytest.mark.parametrize("value,expected", [
    (" https://a.test/image.png ,http://b.test/video.mp4,https://a.test/image.png ",
     ["https://a.test/image.png", "http://b.test/video.mp4"]),
    ("asset://reviewed,not a URL", []), ("", []),
])
def test_shared_result_url_parser_preserves_existing_result_contract(value, expected):
    assert extract_result_urls(value) == expected
