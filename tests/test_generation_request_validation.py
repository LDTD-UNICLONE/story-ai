from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.core.exceptions import AppException
from app.schemas.conversation import ConversationSendMessageRequest
from app.services.conversation import service as conversations
from app.services.generation.runner import validate_model_request
from app.services.projects.canvas_inputs import compile_inputs


def model(vendor="comfly", kind="video", model_id="sora-2", **overrides):
    return SimpleNamespace(
        id=uuid4(), vendor=vendor, model_type=kind, model_id=model_id,
        nickname="test model", configuration=overrides.get("configuration", {}),
    )


def prepare_conversation(monkeypatch, selected_model):
    conversation = SimpleNamespace(
        id=uuid4(), title="test", ai_model_id=selected_model.id,
        conversation_type=selected_model.model_type,
    )
    monkeypatch.setattr(conversations, "get_conversation_or_404", AsyncMock())
    monkeypatch.setattr(conversations, "expire_stale_task_records", AsyncMock())
    monkeypatch.setattr(conversations, "_lock_conversation", AsyncMock(return_value=conversation))
    monkeypatch.setattr(conversations, "_find_idempotent_text_submission", AsyncMock(return_value=None))
    monkeypatch.setattr(
        conversations, "get_enabled_conversation_model_or_404",
        AsyncMock(return_value=selected_model),
    )
    balance = AsyncMock(side_effect=AssertionError("Invalid requests must fail before billing"))
    monkeypatch.setattr(conversations, "ensure_model_minimum_balance", balance)
    return conversation, balance


@pytest.mark.parametrize("vendor", ["comfly", "模型服务"])
async def test_conversation_rejects_invalid_video_duration_before_billing(monkeypatch, vendor):
    selected_model = model(vendor=vendor)
    conversation, balance = prepare_conversation(monkeypatch, selected_model)
    with pytest.raises(AppException) as error:
        await conversations.send_conversation_message(
            None, conversation.id, SimpleNamespace(id=uuid4()),
            ConversationSendMessageRequest(
                content="镜头缓缓向前", extra={"generation_mode": "text_to_video", "duration": 6},
            ),
        )
    assert error.value.code == 40021
    balance.assert_not_awaited()


async def test_canvas_rejects_same_invalid_video_duration():
    selected_model = model()
    node = SimpleNamespace(id=uuid4(), kind="video", content={
        "text": "镜头缓缓向前",
        "generation": {"ai_model_id": str(selected_model.id), "parameters": {"duration": 6}},
    })
    with pytest.raises(AppException) as error:
        await compile_inputs(None, uuid4(), uuid4(), node, {node.id: node}, {}, selected_model)
    assert error.value.code == 40021


@pytest.mark.parametrize("vendor", ["apimart", "comfly", "模型服务"])
@pytest.mark.parametrize("kind,model_id,extra", [
    ("text", "gpt-4o", {"max_tokens": 100, "messages": [{"role": "user", "content": "你好"}]}),
    ("image", "gpt-image-2", {"n": 1}),
])
def test_valid_text_and_image_inputs_are_not_mutated(vendor, kind, model_id, extra):
    original = deepcopy(extra)
    validate_model_request(model(vendor, kind, model_id), kind, "你好", extra)
    assert extra == original


@pytest.mark.parametrize("vendor", ["apimart", "comfly", "模型服务"])
def test_invalid_text_parameters_are_rejected(vendor):
    with pytest.raises(AppException) as error:
        validate_model_request(model(vendor, "text", "gpt-4o"), "text", "你好", {"temperature": "invalid"})
    assert error.value.status_code == 400


@pytest.mark.parametrize("vendor,model_id", [
    ("apimart", "seedance-2.0"), ("comfly", "sora-2"),
    ("模型服务", "sora-2"), ("volcengine_ark", "doubao-seedance-2-0-260128"),
])
def test_valid_video_inputs_are_not_mutated(vendor, model_id):
    extra = {"generation_mode": "text_to_video", "video_mode": "text_to_video", "duration": 8}
    original = deepcopy(extra)
    validate_model_request(model(vendor, "video", model_id), "video", "镜头缓缓向前", extra)
    assert extra == original


def test_video_validation_uses_server_capabilities_instead_of_client_override():
    selected_model = model(model_id="custom-video", configuration={"request": {"capabilities": {"durations": [4]}}})
    extra = {"video_mode": "text_to_video", "duration": 8, "_model_capabilities": {"durations": [8]}}
    original = deepcopy(extra)
    with pytest.raises(AppException) as error:
        validate_model_request(selected_model, "video", "镜头缓缓向前", extra)
    assert error.value.code == 40021
    assert extra == original


@pytest.mark.parametrize("entry", ["conversation", "canvas"])
@pytest.mark.parametrize("case,code", [("unknown_provider", 40005), ("maintenance", 50301)])
async def test_unavailable_models_are_rejected_by_both_entries(monkeypatch, entry, case, code):
    selected_model = model(kind="image", model_id="gpt-image-2")
    if case == "unknown_provider":
        selected_model.vendor = "unknown"
    else:
        selected_model.configuration = {"operations": {"status": "maintenance"}}
    if entry == "conversation":
        conversation, balance = prepare_conversation(monkeypatch, selected_model)
        with pytest.raises(AppException) as error:
            await conversations.send_conversation_message(
                None, conversation.id, SimpleNamespace(id=uuid4()),
                ConversationSendMessageRequest(content="画一座山"),
            )
        balance.assert_not_awaited()
    else:
        node = SimpleNamespace(id=uuid4(), kind="image", content={
            "text": "画一座山", "generation": {"ai_model_id": str(selected_model.id)},
        })
        with pytest.raises(AppException) as error:
            await compile_inputs(None, uuid4(), uuid4(), node, {node.id: node}, {}, selected_model)
    assert error.value.code == code
