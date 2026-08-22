from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.core.timezone import beijing_datetime
from app.integrations import apimart
from app.services.apimart_private_avatars import (
    complete_private_avatar_review,
    mark_private_avatar_ready_for_resume,
    prepare_private_avatar_references,
)


class _ScalarResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return self

    def all(self):
        return list(self._values)


@pytest.mark.asyncio
async def test_ready_private_avatar_replaces_character_url() -> None:
    user_id = uuid4()
    source_url = "https://cdn.example/character.png"
    record = SimpleNamespace(user_id=user_id, extra={})
    approved = SimpleNamespace(
        source_fingerprint=("efe2450af3574b9423dddd98fb96c0551ae1412396bb5d6e0fe40b373006b226"),
        source_url=source_url,
        provider_asset_url="asset://character-1",
        status="ready",
    )
    db = SimpleNamespace(execute=AsyncMock(return_value=_ScalarResult([approved])))

    prepared = await prepare_private_avatar_references(
        db,
        record,
        "seedance-2.0",
        {
            "image_urls": [source_url, "https://cdn.example/scene.png"],
            "private_avatar_image_urls": [source_url],
        },
    )

    assert prepared["image_urls"] == [
        "asset://character-1",
        "https://cdn.example/scene.png",
    ]


@pytest.mark.asyncio
async def test_missing_private_avatar_defers_video_task(monkeypatch) -> None:
    captured = {}
    record = SimpleNamespace(
        user_id=uuid4(),
        status="running",
        provider_vendor=None,
        provider_task_id=None,
        provider_status=None,
        provider_submitted_at=None,
        next_reconcile_at=None,
        extra={},
    )
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[_ScalarResult([]), SimpleNamespace()]),
        flush=AsyncMock(),
    )

    async def create_private_avatar_assets(*args, **kwargs):
        captured.update(kwargs)
        return {
            "code": 200,
            "data": {"id": "avatar-task-1", "status": "processing", "progress": 10},
        }

    monkeypatch.setattr(apimart, "create_private_avatar_assets", create_private_avatar_assets)

    prepared = await prepare_private_avatar_references(
        db,
        record,
        "seedance-2.5",
        {
            "image_urls": ["https://cdn.example/character.png"],
            "private_avatar": True,
        },
    )

    assert prepared is None
    assert record.provider_task_id == "avatar-task-1"
    assert record.extra["provider_stage"] == "private_avatar_review"
    assert record.extra["private_avatar"]["progress_percent"] == 10
    assert captured["model"] == "seedance-2.5"
    db.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_private_avatar_partial_failure_keeps_approved_asset() -> None:
    user_id = uuid4()
    first = SimpleNamespace(
        user_id=user_id,
        source_fingerprint="fingerprint-a",
        source_url="https://cdn.example/a.png",
        provider_task_id="avatar-task-1",
        provider_asset_id=None,
        provider_asset_url=None,
        status="processing",
        progress_percent=10,
        error_message=None,
        extra={"submission_index": 0},
    )
    second = SimpleNamespace(
        user_id=user_id,
        source_fingerprint="fingerprint-b",
        source_url="https://cdn.example/b.png",
        provider_task_id="avatar-task-1",
        provider_asset_id=None,
        provider_asset_url=None,
        status="processing",
        progress_percent=10,
        error_message=None,
        extra={"submission_index": 1},
    )
    record = SimpleNamespace(
        user_id=user_id,
        provider_task_id="avatar-task-1",
        extra={
            "private_avatar": {
                "task_id": "avatar-task-1",
                "required_fingerprints": ["fingerprint-a", "fingerprint-b"],
            }
        },
    )
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _ScalarResult([first, second]),
                _ScalarResult([first, second]),
            ]
        )
    )

    outcome = await complete_private_avatar_review(
        db,
        record,
        {
            "task_status": "failed",
            "provider_response": {
                "result": {
                    "assets": [
                        {
                            "asset_id": "asset-a",
                            "asset_url": "asset://asset-a",
                            "status": "Active",
                        },
                        {"asset_id": "asset-b", "status": "Failed"},
                    ]
                }
            },
        },
    )

    assert outcome.failed_reason
    assert outcome.approved_urls == {"https://cdn.example/a.png": "asset://asset-a"}
    assert first.status == "ready"
    assert second.status == "failed"


def test_completed_private_avatar_review_prepares_original_task_for_resume() -> None:
    record = SimpleNamespace(
        status="running",
        provider_task_id="avatar-task-1",
        provider_status="completed",
        provider_submitted_at=beijing_datetime(),
        last_reconcile_at=None,
        next_reconcile_at=beijing_datetime(),
        reconcile_attempts=2,
        extra={
            "provider_stage": "private_avatar_review",
            "private_avatar": {"task_id": "avatar-task-1", "progress_percent": 90},
            "last_provider_task_status": {"task_id": "avatar-task-1"},
        },
    )

    mark_private_avatar_ready_for_resume(record)

    assert record.status == "pending"
    assert record.provider_task_id is None
    assert record.provider_status is None
    assert record.next_reconcile_at is None
    assert record.extra["provider_stage"] == "video_generation_pending"
    assert record.extra["private_avatar"]["progress_percent"] == 100
    assert record.extra["private_avatar_resume_required"] is True
    assert "last_provider_task_status" not in record.extra
