from types import SimpleNamespace

import pytest

from app.integrations import comfly
from app.services.generation.runner import run_model


@pytest.mark.asyncio
async def test_comfly_generation_receives_business_task_idempotency_key(monkeypatch) -> None:
    captured = {}

    async def create_chat_completion(_model, _prompt, _extra, *, idempotency_key=None):
        captured["idempotency_key"] = idempotency_key
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(comfly, "create_chat_completion", create_chat_completion)
    model = SimpleNamespace(model_id="text-model", vendor="comfly", capabilities={})

    result = await run_model(
        model,
        "text",
        "hello",
        {},
        idempotency_key="task-record-123",
    )

    assert result.content == "ok"
    assert captured["idempotency_key"] == "task-record-123"


def test_comfly_idempotency_key_is_sent_as_header(monkeypatch) -> None:
    monkeypatch.setattr(comfly, "_api_key", lambda: "secret")

    headers = comfly._headers("task-record-123")

    assert headers["Idempotency-Key"] == "task-record-123"
