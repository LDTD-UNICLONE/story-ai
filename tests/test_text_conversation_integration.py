import os
import re
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401
from app.core.config import settings
from app.core.exceptions import AppException
from app.db.base import Base
from app.models.ai_model import AiModel
from app.models.conversation import Conversation, ConversationMessage
from app.models.task_record import UserTaskRecord
from app.models.user import User
from app.schemas.conversation import ConversationSendMessageRequest
from app.services import conversations as conversation_service


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_DB_INTEGRATION_TESTS") != "1",
        reason="set RUN_DB_INTEGRATION_TESTS=1 to run PostgreSQL integration tests",
    ),
]


@pytest.fixture
async def text_conversation_db():
    schema_name = f"text_chat_{uuid4().hex}"
    assert re.fullmatch(r"text_chat_[0-9a-f]{32}", schema_name)
    admin_engine = create_async_engine(settings.database_url, poolclass=NullPool)
    test_engine = create_async_engine(
        settings.database_url,
        poolclass=NullPool,
        execution_options={"schema_translate_map": {None: schema_name}},
    )
    async with admin_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
    try:
        async with test_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(
            test_engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        async with session_factory() as session:
            user = User(
                id=uuid4(),
                account=f"text-chat-{uuid4().hex}",
                password_hash="test",
                nickname="Text Chat",
                is_enabled=True,
                points_balance=100,
            )
            model = AiModel(
                id=uuid4(),
                nickname="Text Chat Model",
                model_id=f"text-chat-model-{uuid4().hex}",
                vendor="integration",
                model_type="text",
                is_enabled=True,
                configuration={},
            )
            conversation = Conversation(
                id=uuid4(),
                user_id=user.id,
                title="多轮文本测试",
                conversation_type="text",
                ai_model_id=model.id,
                is_enabled=True,
            )
            session.add_all([user, model])
            await session.flush()
            session.add(conversation)
            await session.commit()
            yield SimpleNamespace(
                session=session,
                user=user,
                model=model,
                conversation=conversation,
            )
    finally:
        await test_engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema_name}" CASCADE'))
        await admin_engine.dispose()


@pytest.mark.asyncio
async def test_text_turn_is_idempotent_serialized_and_retryable(
    text_conversation_db,
    monkeypatch,
) -> None:
    submitted = []
    monkeypatch.setattr(
        conversation_service.run_conversation_generation,
        "apply_async",
        lambda **kwargs: submitted.append(kwargs),
    )
    db = text_conversation_db.session
    payload = ConversationSendMessageRequest(
        content="先介绍一下女主角",
        client_message_id="web-turn-001",
    )

    user_message, assistant_message, _ = await conversation_service.send_conversation_message(
        db,
        text_conversation_db.conversation.id,
        text_conversation_db.user,
        payload,
    )
    replay_user, replay_assistant, _ = await conversation_service.send_conversation_message(
        db,
        text_conversation_db.conversation.id,
        text_conversation_db.user,
        payload,
    )

    assert replay_user.id == user_message.id
    assert replay_assistant.id == assistant_message.id
    assert user_message.turn_id == assistant_message.turn_id
    assert (user_message.sequence_no, assistant_message.sequence_no) == (1, 2)
    assert (user_message.status, assistant_message.status) == ("success", "pending")
    assert len(submitted) == 1

    message_count = await db.scalar(
        select(func.count()).select_from(ConversationMessage).where(
            ConversationMessage.conversation_id == text_conversation_db.conversation.id
        )
    )
    task_count = await db.scalar(
        select(func.count()).select_from(UserTaskRecord).where(
            UserTaskRecord.business_id == text_conversation_db.conversation.id
        )
    )
    assert (message_count, task_count) == (2, 1)

    with pytest.raises(AppException) as idempotency_error:
        await conversation_service.send_conversation_message(
            db,
            text_conversation_db.conversation.id,
            text_conversation_db.user,
            ConversationSendMessageRequest(
                content="相同幂等键的另一条内容",
                client_message_id="web-turn-001",
            ),
        )
    assert idempotency_error.value.code == 40997

    with pytest.raises(AppException) as active_error:
        await conversation_service.send_conversation_message(
            db,
            text_conversation_db.conversation.id,
            text_conversation_db.user,
            ConversationSendMessageRequest(
                content="第二条消息",
                client_message_id="web-turn-002",
            ),
        )
    assert active_error.value.code == 40996

    task_record = await db.get(
        UserTaskRecord,
        UUID(assistant_message.extra["task_record_id"]),
    )
    task_record.status = "failed"
    assistant_message.status = "failed"
    assistant_message.extra = {**assistant_message.extra, "task_status": "failed"}
    await db.commit()

    retried_user, retried_assistant, _ = await conversation_service.retry_text_conversation_turn(
        db,
        text_conversation_db.conversation.id,
        user_message.id,
        text_conversation_db.user,
    )

    assert retried_user.id == user_message.id
    assert retried_assistant.turn_id == user_message.turn_id
    assert retried_assistant.sequence_no == 3
    assert retried_assistant.status == "pending"
    assert len(submitted) == 2

    retried_task = await db.get(
        UserTaskRecord,
        UUID(retried_assistant.extra["task_record_id"]),
    )
    retried_task.status = "success"
    retried_assistant.status = "success"
    retried_assistant.content = "女主角是一名律师。"
    retried_assistant.extra = {**retried_assistant.extra, "task_status": "success"}
    await db.commit()

    context = await conversation_service._build_text_context_messages(
        db,
        text_conversation_db.conversation.id,
        "继续",
        {},
    )
    assert context == [
        {"role": "user", "content": "先介绍一下女主角"},
        {"role": "assistant", "content": "女主角是一名律师。"},
        {"role": "user", "content": "继续"},
    ]


@pytest.mark.asyncio
async def test_text_base_points_only_gate_minimum_balance(
    text_conversation_db,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        conversation_service.run_conversation_generation,
        "apply_async",
        lambda **_kwargs: None,
    )
    db = text_conversation_db.session
    user = text_conversation_db.user
    model = text_conversation_db.model
    model.configuration = {
        "version": 1,
        "billing": {"base_points": 10},
    }
    user.points_balance = 9
    await db.commit()

    with pytest.raises(AppException) as insufficient:
        await conversation_service.send_conversation_message(
            db,
            text_conversation_db.conversation.id,
            user,
            ConversationSendMessageRequest(
                content="余额不足时不能提交",
                client_message_id="minimum-balance-rejected",
            ),
        )
    assert insufficient.value.code == 40003

    user.points_balance = 10
    await db.commit()
    _, assistant_message, points_cost = await conversation_service.send_conversation_message(
        db,
        text_conversation_db.conversation.id,
        user,
        ConversationSendMessageRequest(
            content="余额达到门槛后可以提交",
            client_message_id="minimum-balance-accepted",
        ),
    )

    task_record = await db.get(
        UserTaskRecord,
        UUID(assistant_message.extra["task_record_id"]),
    )
    await db.refresh(user)
    assert points_cost == 0
    assert task_record.points_cost == 0
    assert task_record.points_transaction_id is None
    assert user.points_balance == 10


@pytest.mark.asyncio
async def test_deleting_conversation_interrupts_active_generation(
    text_conversation_db,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        conversation_service.run_conversation_generation,
        "apply_async",
        lambda **kwargs: None,
    )
    db = text_conversation_db.session
    _, assistant_message, _ = await conversation_service.send_conversation_message(
        db,
        text_conversation_db.conversation.id,
        text_conversation_db.user,
        ConversationSendMessageRequest(
            content="生成一段介绍",
            client_message_id="delete-active-turn",
        ),
    )
    task_record_id = UUID(assistant_message.extra["task_record_id"])

    deleted = await conversation_service.delete_conversation(
        db,
        text_conversation_db.conversation.id,
        text_conversation_db.user.id,
    )

    await db.refresh(assistant_message)
    task_record = await db.get(UserTaskRecord, task_record_id)
    assert deleted.is_enabled is False
    assert task_record.status == "failed"
    assert task_record.extra["interrupted"] is True
    assert assistant_message.status == "failed"
    assert assistant_message.extra["task_status"] == "failed"


@pytest.mark.asyncio
async def test_deleting_user_message_interrupts_its_active_generation(
    text_conversation_db,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        conversation_service.run_conversation_generation,
        "apply_async",
        lambda **kwargs: None,
    )
    db = text_conversation_db.session
    user_message, assistant_message, _ = await conversation_service.send_conversation_message(
        db,
        text_conversation_db.conversation.id,
        text_conversation_db.user,
        ConversationSendMessageRequest(
            content="这条消息立即删除",
            client_message_id="delete-user-message-turn",
        ),
    )
    task_record_id = UUID(assistant_message.extra["task_record_id"])

    await conversation_service.delete_conversation_message(
        db,
        text_conversation_db.conversation.id,
        user_message.id,
        text_conversation_db.user.id,
    )

    await db.refresh(assistant_message)
    task_record = await db.get(UserTaskRecord, task_record_id)
    assert task_record.status == "failed"
    assert task_record.extra["interrupted"] is True
    assert assistant_message.status == "failed"
    assert assistant_message.extra["task_status"] == "failed"
