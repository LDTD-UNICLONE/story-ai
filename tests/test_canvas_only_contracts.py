from app.api.v1.router import api_router
from app.api.v1.endpoints.project_dependencies import require_standard_project
from app.api.v1.endpoints.canvas_import_records import router
from app.schemas.project_canvas import CanvasNodeInput
from app.schemas.project_media import ProjectMediaImport
from pydantic import ValidationError
import pytest
from uuid import uuid4
from fastapi import FastAPI


def test_only_canvas_endpoints_are_published_for_standard_project_content():
    app = FastAPI()
    app.include_router(api_router)
    paths = set(app.openapi()["paths"])
    project_paths = {path for path in paths if path.startswith('/projects/')}
    forbidden = ('/chapters', '/characters', '/scenes', '/props', '/assets', '/image-reviews',
                 '/canvas-content', '/apply-to-project', '/agent-productions')
    assert not [path for path in project_paths if any(part in path for part in forbidden)]
    assert '/projects/{project_id}/canvases/{canvas_id}/imports' not in paths
    assert '/projects/{project_id}/import-records/{record_id}' in paths
    assert any(path.startswith('/agent') for path in paths)
    for route in router.routes:
        assert route.methods == {'GET'}
        assert require_standard_project in {dep.call for dep in route.dependant.dependencies}


def test_import_provenance_is_read_only_and_media_import_is_user_image_only():
    with pytest.raises(ValidationError):
        CanvasNodeInput(id=uuid4(), kind='image', import_record_id=uuid4())
    for source_type in ('generated_image', 'generated_last_frame'):
        with pytest.raises(ValidationError):
            ProjectMediaImport(source_type=source_type, source_id=uuid4())
