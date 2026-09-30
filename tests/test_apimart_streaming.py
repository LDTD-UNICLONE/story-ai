import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import Response

from app.api.v1.endpoints import conversations as conversation_endpoint
from app.api.v1.endpoints.conversations import _sse_event
from app.models.conversation import ConversationMessage
from app.models.task_record import UserTaskRecord
from app.tasks.model_generation import _build_apimart_text_delta_callback


class _CommitDb:
    def __init__(self):
        self.commit_count = 0

    async def commit(self):
        self.commit_count += 1

    async def refresh(self, _record):
        pass


@pytest.mark.asyncio
async def test_apimart_stream_deltas_are_buffered_before_database_flush(monkeypatch) -> None:
    db = _CommitDb()
    assistant_message = SimpleNamespace(content="任务已提交", extra={})
    monkeypatch.setattr(
        "app.tasks.model_generation.lock_active_task", AsyncMock(return_value=True)
    )
    callback = _build_apimart_text_delta_callback(db, assistant_message, SimpleNamespace(id=uuid4(), user_id=uuid4()))

    await callback("甲" * 64)

    assert db.commit_count == 0
    assert assistant_message.content == "任务已提交"

    await callback("乙" * 64)

    assert db.commit_count == 1
    assert assistant_message.content == "甲" * 64 + "乙" * 64
    assert assistant_message.extra["stream_started"] is True
    assert assistant_message.extra["streamed_character_count"] == 128


def test_conversation_sse_event_uses_named_json_event() -> None:
    event = _sse_event("delta", {"delta": "你好", "status": "running"})
    lines = event.strip().splitlines()

    assert lines[0] == "event: delta"
    assert json.loads(lines[1].removeprefix("data: ")) == {
        "delta": "你好",
        "status": "running",
    }


@pytest.mark.asyncio
async def test_conversation_stream_releases_request_transaction_before_response(
    monkeypatch,
) -> None:
    task_id = uuid4()
    message_id = uuid4()
    task_record = SimpleNamespace(id=task_id)
    assistant_message = SimpleNamespace(id=message_id)
    db = SimpleNamespace(rollback=AsyncMock())

    async def get_status(*args, **kwargs):
        return task_record, assistant_message

    monkeypatch.setattr(
        conversation_endpoint,
        "get_conversation_generation_task_status",
        get_status,
    )

    response = await conversation_endpoint.stream_my_conversation_generation_task(
        conversation_id=uuid4(),
        task_record_id=task_id,
        request=SimpleNamespace(),
        db=db,
        current_user=SimpleNamespace(id=uuid4()),
    )

    db.rollback.assert_awaited_once()
    await response.body_iterator.aclose()


@pytest.mark.asyncio
async def test_conversation_task_status_exposes_top_level_progress(monkeypatch) -> None:
    task_id = uuid4()
    conversation_id = uuid4()
    user_id = uuid4()
    now = datetime.now(timezone.utc)
    task_record = SimpleNamespace(
        id=task_id,
        status="running",
        generation_type="video",
        result=None,
        extra={"progress_percent": 46},
        created_at=now,
        updated_at=now,
    )

    async def get_status(*args, **kwargs):
        return task_record, None

    monkeypatch.setattr(
        conversation_endpoint,
        "get_conversation_generation_task_status",
        get_status,
    )

    result = await conversation_endpoint.my_conversation_generation_task(
        conversation_id=conversation_id,
        task_record_id=task_id,
        response=Response(),
        wait_seconds=0,
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=user_id),
    )

    assert result.data["progress_percent"] == 46


@pytest.mark.asyncio
async def test_conversation_stream_starts_with_snapshot_then_sends_delta(monkeypatch) -> None:
    task_id = uuid4()
    message_id = uuid4()
    first_content = "甲" * 128
    states = [
        (
            SimpleNamespace(id=task_id, status="running", result=None, extra={}),
            SimpleNamespace(
                id=message_id,
                content=first_content,
                extra={"stream_started": True},
            ),
        ),
        (
            SimpleNamespace(id=task_id, status="success", result=first_content + "乙", extra={}),
            SimpleNamespace(
                id=message_id,
                content=first_content + "乙",
                extra={"stream_started": True},
            ),
        ),
    ]

    class Session:
        def __init__(self, state):
            self.task, self.message = state

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

        async def get(self, model, record_id):
            if model is UserTaskRecord:
                return self.task
            if model is ConversationMessage:
                return self.message
            return None

    class SessionFactory:
        def __call__(self):
            return Session(states.pop(0))

    class Request:
        async def is_disconnected(self):
            return False

    async def no_sleep(seconds):
        return None

    monkeypatch.setattr(conversation_endpoint, "AsyncSessionLocal", SessionFactory())
    monkeypatch.setattr("app.services.generation.task_events.asyncio.sleep", no_sleep)

    events = [
        item
        async for item in conversation_endpoint._conversation_generation_event_stream(
            Request(),
            task_record_id=task_id,
            assistant_message_id=message_id,
        )
    ]

    event_names = [item.splitlines()[0] for item in events]
    assert event_names == [
        "event: status",
        "event: snapshot",
        "event: status",
        "event: delta",
        "event: completed",
    ]
    delta_data = json.loads(events[3].splitlines()[1].removeprefix("data: "))
    assert delta_data["delta"] == "乙"


@pytest.mark.asyncio
async def test_conversation_video_stream_sends_progress_only_when_changed(monkeypatch) -> None:
    task_id = uuid4()
    message_id = uuid4()
    states = [
        SimpleNamespace(
            id=task_id,
            status="running",
            generation_type="video",
            result=None,
            extra={"progress_percent": 12},
        ),
        SimpleNamespace(
            id=task_id,
            status="running",
            generation_type="video",
            result=None,
            extra={"progress_percent": 12},
        ),
        SimpleNamespace(
            id=task_id,
            status="running",
            generation_type="video",
            result=None,
            extra={"progress_percent": 46},
        ),
        SimpleNamespace(
            id=task_id,
            status="success",
            generation_type="video",
            result="https://cdn.example/result.mp4",
            extra={"progress_percent": 46},
        ),
    ]
    message = SimpleNamespace(
        id=message_id,
        content="https://cdn.example/result.mp4",
        extra={},
    )

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

        async def get(self, model, record_id):
            if model is UserTaskRecord:
                return states.pop(0)
            if model is ConversationMessage:
                return message
            return None

    class SessionFactory:
        def __call__(self):
            return Session()

    class Request:
        async def is_disconnected(self):
            return False

    async def no_sleep(seconds):
        return None

    monkeypatch.setattr(conversation_endpoint, "AsyncSessionLocal", SessionFactory())
    monkeypatch.setattr("app.services.generation.task_events.asyncio.sleep", no_sleep)

    events = [
        item
        async for item in conversation_endpoint._conversation_generation_event_stream(
            Request(),
            task_record_id=task_id,
            assistant_message_id=message_id,
        )
    ]

    progress_events = [item for item in events if item.startswith("event: progress\n")]
    assert [
        json.loads(item.splitlines()[1].removeprefix("data: "))["progress_percent"]
        for item in progress_events
    ] == [12, 46, 100]
    completed = next(item for item in events if item.startswith("event: completed\n"))
    assert json.loads(completed.splitlines()[1].removeprefix("data: "))["progress_percent"] == 100
