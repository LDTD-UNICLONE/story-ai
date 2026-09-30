from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.core.exceptions import AppException
from app.schemas.project_canvas import CanvasCreate, CanvasEdgeInput, CanvasNodeInput, CanvasPatch
from app.services.projects.canvases import _validate_graph


@pytest.mark.parametrize(
    "payload",
    [
        {"expected_revision": True, "name": "test"},
        {"expected_revision": 1, "name": "  "},
        {"expected_revision": 1, "name": None},
        {"expected_revision": 1, "viewport": None},
        {"expected_revision": 1},
        {"expected_revision": 1, "viewport": {"zoom": 0}},
        {"expected_revision": 1, "status": "ready"},
    ],
)
def test_canvas_patch_rejects_invalid_contract(payload):
    with pytest.raises(ValidationError):
        CanvasPatch(**payload)


@pytest.mark.parametrize(
    "changes",
    [
        {"x": float("nan")},
        {"y": float("inf")},
        {"width": 0},
        {"kind": "audio"},
        {"content_revision": 100},
        {"content": {"text": "a" * 10001}},
        {"content": {"review_status": "ready"}},
        {"content": {"result_url": "https://example.com/forged.png"}},
    ],
)
def test_node_cannot_store_unbounded_or_server_owned_fields(changes):
    with pytest.raises(ValidationError):
        CanvasNodeInput(**{"id": uuid4(), "kind": "image", **changes})


def test_operation_limits_and_conflicting_ids():
    node = CanvasNodeInput(id=uuid4(), kind="text")
    for changes in (
        {"upsert_nodes": [node, node]},
        {"upsert_nodes": [node], "delete_node_ids": [node.id]},
        {"delete_node_ids": [uuid4() for _ in range(501)]},
        {
            "delete_node_ids": [uuid4() for _ in range(300)],
            "delete_edge_ids": [uuid4() for _ in range(201)],
        },
        {
            "upsert_nodes": [
                CanvasNodeInput(id=uuid4(), kind="text", content={"text": "文" * 10000})
                for _ in range(80)
            ]
        },
    ):
        with pytest.raises(ValidationError):
            CanvasPatch(expected_revision=1, **changes)
    assert CanvasCreate(name="  测试  ").name == "测试"


def test_graph_limit_and_cross_canvas_parent():
    nodes = {
        node.id: node for node in (CanvasNodeInput(id=uuid4(), kind="text") for _ in range(1001))
    }
    with pytest.raises(AppException, match="1000"):
        _validate_graph(nodes, {})
    node = CanvasNodeInput(id=uuid4(), kind="image", parent_id=uuid4())
    with pytest.raises(AppException, match="父节点"):
        _validate_graph({node.id: node}, {})


def test_group_and_edge_cycles_rejected():
    a = CanvasNodeInput(id=uuid4(), kind="group")
    b = CanvasNodeInput(id=uuid4(), kind="group", parent_id=a.id)
    a.parent_id = b.id
    with pytest.raises(AppException, match="循环"):
        _validate_graph({a.id: a, b.id: b}, {})
    a.kind = b.kind = "text"
    a.parent_id = b.parent_id = None
    edges = [
        CanvasEdgeInput(id=uuid4(), source_id=a.id, target_id=b.id),
        CanvasEdgeInput(id=uuid4(), source_id=b.id, target_id=a.id),
    ]
    with pytest.raises(AppException, match="循环"):
        _validate_graph({a.id: a, b.id: b}, {e.id: e for e in edges})


def test_canvas_routes_keep_standard_project_guard():
    from fastapi import FastAPI
    from app.api.v1.endpoints.project_canvases import router
    from app.api.v1.endpoints.project_dependencies import require_standard_project
    from app.api.v1.router import api_router

    for route in router.routes:
        assert require_standard_project in {dep.call for dep in route.dependant.dependencies}
    app = FastAPI()
    app.include_router(api_router)
    assert "/projects/{project_id}/canvases" in app.openapi()["paths"]
