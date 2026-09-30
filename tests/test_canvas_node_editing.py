from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.project_canvas import CanvasPatch


@pytest.mark.parametrize("fields", [
    {}, {"title": None}, {"content": None}, {"content": {}}, {"content": {"text": None}},
    {"kind": "video"}, {"x": 10}, {"content_revision": 1}, {"selected_generation_id": uuid4()},
    {"content": {"generation": {"parameters": {}}}},
])
def test_partial_node_edit_rejects_empty_readonly_and_invalid_fields(fields):
    with pytest.raises(ValidationError):
        CanvasPatch(expected_revision=1, update_nodes=[{"id": uuid4(), **fields}])


def test_node_edit_conflicts_and_shared_operation_limit():
    id_ = uuid4()
    change = {"id": id_, "title": "Rename"}
    for body in (
        {"update_nodes": [change, change]},
        {"update_nodes": [change], "upsert_nodes": [{"id": id_, "kind": "text"}]},
        {"update_nodes": [change], "delete_node_ids": [id_]},
        {"update_nodes": [change], "delete_edge_ids": [uuid4() for _ in range(500)]},
    ):
        with pytest.raises(ValidationError):
            CanvasPatch(expected_revision=1, **body)
    payload = CanvasPatch(expected_revision=1, update_nodes=[{
        "id": id_, "title": "", "media_id": None, "parent_id": None,
        "content": {"text": "", "generation": None},
    }])
    assert payload.update_nodes[0].content.model_fields_set == {"text", "generation"}
