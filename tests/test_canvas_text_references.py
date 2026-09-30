from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.project_canvas import CanvasEdgeInput


@pytest.mark.parametrize('fields', [
    {'input': 'text'}, {'input': 'text', 'text_source': 'result', 'media_id': uuid4()},
    {'text_source': 'input'}, {'refresh_text': True},
    {'input': 'text', 'text_source': 'input', 'refresh_text': 'true'},
    {'input': 'text', 'text_source': 'input', 'text_snapshot': {'text': 'forged'}},
])
def test_text_edge_rejects_ambiguous_or_forged_sources(fields):
    with pytest.raises(ValidationError):
        CanvasEdgeInput(id=uuid4(), source_id=uuid4(), target_id=uuid4(), **fields)
