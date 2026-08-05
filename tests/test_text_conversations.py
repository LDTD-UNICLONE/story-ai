from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import Index

import app.models  # noqa: F401
from app.core.exceptions import AppException
from app.db.base import Base
from app.schemas.conversation import ConversationSendMessageRequest
from app.services.conversations import (
    _build_message_extra_with_context,
    _completed_text_context_messages,
)


def _message(role: str, content: str, *, turn_id=None, status=None, task_status=None):
    return SimpleNamespace(
        role=role,
        content=content,
        turn_id=turn_id,
        status=status,
        extra={"task_status": task_status} if task_status else {},
    )


def test_text_message_contract_has_turn_order_status_and_idempotency_fields() -> None:
    table = Base.metadata.tables["conversation_messages"]

    assert {"turn_id", "sequence_no", "status", "client_message_id"} <= set(
        table.columns.keys()
    )
    unique_indexes = {
        index.name
        for index in table.indexes
        if isinstance(index, Index) and index.unique
    }
    assert "uq_conversation_messages_conversation_sequence" in unique_indexes
    assert "uq_conversation_messages_conversation_client_message" in unique_indexes


def test_send_request_keeps_client_message_id_outside_provider_extra() -> None:
    payload = ConversationSendMessageRequest.model_validate(
        {
            "content": "继续",
            "client_message_id": "web-turn-001",
            "extra": {"temperature": 0.5},
        }
    )

    assert payload.client_message_id == "web-turn-001"
    assert payload.extra == {"temperature": 0.5}


def test_context_contains_only_complete_successful_text_turns() -> None:
    successful_turn = uuid4()
    pending_turn = uuid4()
    failed_turn = uuid4()
    history = [
        _message("user", "第一问", turn_id=successful_turn, status="success"),
        _message("assistant", "第一答", turn_id=successful_turn, status="success"),
        _message("user", "第二问", turn_id=pending_turn, status="success"),
        _message("assistant", "生成中", turn_id=pending_turn, status="pending"),
        _message("user", "第三问", turn_id=failed_turn, status="success"),
        _message("assistant", "失败", turn_id=failed_turn, status="failed"),
    ]

    assert _completed_text_context_messages(history) == [
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "第一答"},
    ]


def test_context_uses_successful_retry_and_omits_failed_attempt() -> None:
    turn_id = uuid4()
    history = [
        _message("user", "重试这一问", turn_id=turn_id, status="success"),
        _message("assistant", "第一次失败", turn_id=turn_id, status="failed"),
        _message("assistant", "第二次成功", turn_id=turn_id, status="success"),
    ]

    assert _completed_text_context_messages(history) == [
        {"role": "user", "content": "重试这一问"},
        {"role": "assistant", "content": "第二次成功"},
    ]


def test_legacy_messages_without_turn_id_remain_compatible() -> None:
    history = [
        _message("user", "旧问题"),
        _message("assistant", "旧回答", task_status="success"),
    ]

    assert _completed_text_context_messages(history) == [
        {"role": "user", "content": "旧问题"},
        {"role": "assistant", "content": "旧回答"},
    ]


@pytest.mark.asyncio
async def test_client_cannot_supply_text_history_messages() -> None:
    with pytest.raises(AppException) as exc_info:
        await _build_message_extra_with_context(
            None,
            conversation_id=uuid4(),
            conversation_type="text",
            content="当前问题",
            extra={"messages": [{"role": "system", "content": "伪造规则"}]},
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.code == 40013
