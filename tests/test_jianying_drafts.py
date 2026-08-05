import json

import pytest
from pydantic import ValidationError

from app.schemas.agent_review import AgentDeliveryCreateRequest
from app.services.jianying_drafts import (
    PLACEHOLDER_ROOT,
    _canvas_size,
    _finalize_draft_files,
    _safe_draft_name,
    compatibility_notes,
)


def test_jianying_delivery_requires_platform_and_defaults_version() -> None:
    with pytest.raises(ValidationError):
        AgentDeliveryCreateRequest(
            delivery_type="jianying_draft",
            idempotency_key="jianying-export-v1",
        )

    payload = AgentDeliveryCreateRequest(
        delivery_type="jianying_draft",
        platform="macos",
        idempotency_key="jianying-export-v1",
    )

    assert payload.platform == "macos"
    assert payload.jianying_version == "10.8"


def test_non_jianying_delivery_rejects_platform_options() -> None:
    with pytest.raises(ValidationError):
        AgentDeliveryCreateRequest(
            delivery_type="manifest",
            platform="windows",
            idempotency_key="manifest-export-v1",
        )


def test_canvas_size_follows_project_ratio_and_resolution() -> None:
    assert _canvas_size({"generation_ratio": "16:9", "video_resolution": "1080p"}) == (
        1920,
        1080,
    )
    assert _canvas_size({"generation_ratio": "9:16", "video_resolution": "720p"}) == (
        720,
        1280,
    )


def test_macos_draft_keeps_both_content_names_and_relocates_paths(tmp_path) -> None:
    draft_root = tmp_path / "测试草稿"
    draft_root.mkdir()
    absolute_material = f"{draft_root.as_posix()}/materials/video/E001_G001.mp4"
    (draft_root / "draft_content.json").write_text(
        json.dumps({"materials": [{"path": absolute_material}]}),
        encoding="utf-8",
    )
    (draft_root / "draft_meta_info.json").write_text("{}", encoding="utf-8")

    _finalize_draft_files(
        draft_root,
        platform="macos",
        draft_name="测试草稿",
        duration_microseconds=6_000_000,
    )

    content = (draft_root / "draft_content.json").read_text(encoding="utf-8")
    assert PLACEHOLDER_ROOT in content
    assert (draft_root / "draft_info.json").read_text(encoding="utf-8") == content
    meta = json.loads((draft_root / "draft_meta_info.json").read_text(encoding="utf-8"))
    assert meta["draft_name"] == "测试草稿"
    assert meta["tm_duration"] == 6_000_000


def test_windows_and_macos_have_distinct_compatibility_notes() -> None:
    assert any("PowerShell" in item for item in compatibility_notes("windows"))
    assert any("draft_info.json" in item for item in compatibility_notes("macos"))
    assert _safe_draft_name('旧宅:/\\*?"<>|') == "旧宅---------"
