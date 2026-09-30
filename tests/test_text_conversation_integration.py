import os
import re
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401
from app.core.exceptions import AppException
from app.api.v1.endpoints import conversations as conversation_endpoint
from app.db.base import Base
from app.models.ai_model import AiModel
from app.models.conversation import Conversation, ConversationMessage
from app.models.task_record import UserTaskRecord
from app.models.task_dispatch import TaskDispatchOutbox
from app.models.points import UserPointsTransaction
from app.models.user import User
from app.schemas.conversation import ConversationSendMessageRequest
from app.services.conversation import service as conversation_service
from app.services.generation import task_dispatch
from app.services.conversation.text_context import build_text_context_messages


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_DB_INTEGRATION_TESTS") != "1",
        reason="set RUN_DB_INTEGRATION_TESTS=1 to run PostgreSQL integration tests",
    ),
]


@pytest.fixture
async def text_conversation_db(test_database_url):
    schema_name = f"text_chat_{uuid4().hex}"
    assert re.fullmatch(r"text_chat_[0-9a-f]{32}", schema_name)
    admin_engine = create_async_engine(test_database_url, poolclass=NullPool)
    test_engine = create_async_engine(
        test_database_url,
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
                vendor="comfly",
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
        task_dispatch,
        "publish_task_message",
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

    context = await build_text_context_messages(
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
@pytest.mark.parametrize("status", ["pending", "success", "failed"])
async def test_stream_opens_after_releasing_real_database_transaction(
    text_conversation_db, monkeypatch, status,
) -> None:
    monkeypatch.setattr(task_dispatch, "publish_task_message", lambda **kwargs: None)
    data = text_conversation_db
    db = data.session
    _, assistant, _ = await conversation_service.send_conversation_message(
        db, data.conversation.id, data.user,
        ConversationSendMessageRequest(content="测试流式回答"),
    )
    task_id = UUID(assistant.extra["task_record_id"])
    task = await db.get(UserTaskRecord, task_id)
    task.status = status
    task.result = "已完成回答" if status == "success" else "生成失败" if status == "failed" else None
    await db.commit()

    response = await conversation_endpoint.stream_my_conversation_generation_task(
        conversation_id=data.conversation.id,
        task_record_id=task_id,
        request=SimpleNamespace(),
        db=db,
        current_user=data.user,
    )

    assert response.media_type == "text/event-stream"
    assert not db.in_transaction()
    await response.body_iterator.aclose()


@pytest.mark.asyncio
async def test_retry_earlier_turn_does_not_include_later_conversation(
    text_conversation_db, monkeypatch,
) -> None:
    monkeypatch.setattr(task_dispatch, "publish_task_message", lambda **kwargs: None)
    data = text_conversation_db
    db = data.session

    async def finish_turn(content, answer, status):
        user_message, assistant, _ = await conversation_service.send_conversation_message(
            db, data.conversation.id, data.user,
            ConversationSendMessageRequest(content=content),
        )
        task = await db.get(UserTaskRecord, UUID(assistant.extra["task_record_id"]))
        task.status = status
        task.result = answer
        assistant.status = status
        assistant.content = answer
        assistant.extra = {**assistant.extra, "task_status": status}
        await db.commit()
        return user_message

    await finish_turn("先介绍主角", "主角是一名律师。", "success")
    failed_user = await finish_turn("她第一次出庭的经历？", "生成失败", "failed")
    await finish_turn("改为写十年后的结局", "她成为了法官。", "success")

    _, assistant, _ = await conversation_service.retry_text_conversation_turn(
        db, data.conversation.id, failed_user.id, data.user,
    )
    task = await db.get(UserTaskRecord, UUID(assistant.extra["task_record_id"]))
    assert task.extra["user_message_extra"]["messages"] == [
        {"role": "user", "content": "先介绍主角"},
        {"role": "assistant", "content": "主角是一名律师。"},
        {"role": "user", "content": "她第一次出庭的经历？"},
    ]


@pytest.mark.asyncio
async def test_text_base_points_only_gate_minimum_balance(
    text_conversation_db,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        task_dispatch,
        "publish_task_message",
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
        task_dispatch,
        "publish_task_message",
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
        task_dispatch,
        "publish_task_message",
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


@pytest.mark.parametrize("generation_type", ["text", "image"])
async def test_broker_outage_keeps_submission_pending(
    text_conversation_db, monkeypatch, generation_type,
):
    def unavailable(**kwargs):
        raise ConnectionError('test broker unavailable')

    monkeypatch.setattr(task_dispatch, 'publish_task_message', unavailable)
    fixture = text_conversation_db
    fixture.model.model_type = generation_type
    fixture.model.configuration = {"billing": {"base_points": 3}}
    fixture.conversation.conversation_type = generation_type
    await fixture.session.commit()
    _, assistant, points = await conversation_service.send_conversation_message(
        fixture.session, fixture.conversation.id, fixture.user,
        ConversationSendMessageRequest(content='durable task', client_message_id='durable-001'),
    )
    record = await fixture.session.get(UserTaskRecord, UUID(assistant.extra['task_record_id']))
    assert record.status == 'pending'
    assert assistant.extra["task_status"] == "pending"
    if generation_type == "text":
        assert assistant.status == "pending"
    assert points == (3 if generation_type == "image" else 0)
    assert fixture.user.points_balance == 100 - points
    assert await fixture.session.scalar(
        select(func.count()).select_from(UserPointsTransaction)
        .where(UserPointsTransaction.transaction_type == "refund")
    ) == 0
    outbox = await fixture.session.get(TaskDispatchOutbox, record.id)
    assert outbox.args == [str(record.id), str(assistant.id)]
    assert outbox.queue == f"story_ai_{generation_type}"


@pytest.fixture
async def mixed_conversation_models(text_conversation_db):
    data = text_conversation_db
    data.user.points_balance = 1000
    models = {"text": data.model}
    for model_type in ("image", "video"):
        model = AiModel(
            id=uuid4(), nickname=model_type, model_id=f"{model_type}-{uuid4().hex}",
            vendor="comfly", model_type=model_type, is_enabled=True,
            configuration={"billing": {"base_points": 3}},
        )
        data.session.add(model)
        models[model_type] = model
    await data.session.commit()
    return models


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_type", ["text", "image", "video"])
async def test_one_conversation_sends_all_types_with_order_and_text_idempotency(
    text_conversation_db, mixed_conversation_models, monkeypatch, initial_type,
):
    submitted = []
    monkeypatch.setattr(task_dispatch, "publish_task_message", lambda **kwargs: submitted.append(kwargs))
    data = text_conversation_db
    db = data.session
    models = mixed_conversation_models
    data.conversation.conversation_type = initial_type
    data.conversation.ai_model_id = models[initial_type].id
    await db.commit()
    messages = []
    tasks = []
    for index, model_type in enumerate(("text", "image", "video")):
        pair = await conversation_service.send_conversation_message(
            db, data.conversation.id, data.user,
            ConversationSendMessageRequest(
                content=f"生成{model_type}", ai_model_id=models[model_type].id,
                client_message_id="mixed-text-001" if model_type == "text" else None,
            ),
        )
        user_message, assistant, points = pair
        messages.extend((user_message, assistant))
        assert user_message.message_type == assistant.message_type == model_type
        assert (user_message.sequence_no, assistant.sequence_no) == (index * 2 + 1, index * 2 + 2)
        task = await db.get(UserTaskRecord, UUID(assistant.extra["task_record_id"]))
        tasks.append(task)
        assert task.generation_type == model_type
        assert submitted[-1]["queue"] == f"story_ai_{model_type}"
        assert points == task.points_cost
        assert (points == 0) == (model_type == "text")

    assert data.conversation.conversation_type == "video"
    assert data.conversation.ai_model_id == models["video"].id
    replay_user, replay_assistant, _ = await conversation_service.send_conversation_message(
        db, data.conversation.id, data.user,
        ConversationSendMessageRequest(content="生成text", client_message_id="mixed-text-001"),
    )
    assert (replay_user.id, replay_assistant.id) == (messages[0].id, messages[1].id)
    with pytest.raises(AppException) as active_error:
        await conversation_service.send_conversation_message(
            db, data.conversation.id, data.user,
            ConversationSendMessageRequest(content="继续聊天", ai_model_id=models["text"].id),
        )
    assert active_error.value.code == 40996

    for order in ("asc", "desc"):
        items, total = await conversation_service.list_conversation_messages(
            db, data.conversation.id, data.user.id, 1, 50, order,
        )
        expected = messages if order == "asc" else list(reversed(messages))
        assert total == 6
        assert [item.id for item in items] == [item.id for item in expected]

    # Media tasks can remain active while the next text turn is submitted.
    tasks[0].status = "success"
    tasks[0].result = "角色设定完成"
    messages[1].status = "success"
    messages[1].content = "角色设定完成"
    messages[1].extra = {**messages[1].extra, "task_status": "success"}
    await db.commit()
    _, assistant, _ = await conversation_service.send_conversation_message(
        db, data.conversation.id, data.user,
        ConversationSendMessageRequest(content="继续聊天", ai_model_id=models["text"].id),
    )
    task = await db.get(UserTaskRecord, UUID(assistant.extra["task_record_id"]))
    assert task.extra["user_message_extra"]["messages"] == [
        {"role": "user", "content": "生成text"},
        {"role": "assistant", "content": "角色设定完成"},
        {"role": "user", "content": "继续聊天"},
    ]


@pytest.mark.asyncio
async def test_mixed_conversation_retries_text_and_queries_media_after_model_switch(
    text_conversation_db, mixed_conversation_models, monkeypatch,
):
    monkeypatch.setattr(task_dispatch, "publish_task_message", lambda **kwargs: None)
    data = text_conversation_db
    db = data.session
    text_user, text_assistant, _ = await conversation_service.send_conversation_message(
        db, data.conversation.id, data.user, ConversationSendMessageRequest(content="原问题"),
    )
    task = await db.get(UserTaskRecord, UUID(text_assistant.extra["task_record_id"]))
    task.status = "failed"
    text_assistant.status = "failed"
    text_assistant.extra = {**text_assistant.extra, "task_status": "failed"}
    await db.commit()
    image_user, image_assistant, _ = await conversation_service.send_conversation_message(
        db, data.conversation.id, data.user,
        ConversationSendMessageRequest(content="画一张图", ai_model_id=mixed_conversation_models["image"].id),
    )
    image_task = await db.get(UserTaskRecord, UUID(image_assistant.extra["task_record_id"]))
    image_task.extra = {**image_task.extra, "provider_task_id": "mixed-image-provider-task"}
    await db.commit()
    _, retry_assistant, _ = await conversation_service.retry_text_conversation_turn(
        db, data.conversation.id, text_user.id, data.user,
    )
    assert retry_assistant.message_type == "text"
    assert retry_assistant.ai_model_id == text_user.ai_model_id
    assert retry_assistant.sequence_no == 5
    with pytest.raises(AppException) as retry_error:
        await conversation_service.retry_text_conversation_turn(
            db, data.conversation.id, image_user.id, data.user,
        )
    assert retry_error.value.code == 40014

    # Selecting video must not hide image tasks from the legacy query endpoint.
    await conversation_service.send_conversation_message(
        db, data.conversation.id, data.user,
        ConversationSendMessageRequest(content="生成视频", ai_model_id=mixed_conversation_models["video"].id),
    )
    result = await conversation_service.query_conversation_generation_task(
        db, data.conversation.id, data.user, "mixed-image-provider-task",
    )
    assert result.extra["task_record_id"] == str(image_task.id)
    # Omitted model uses the most recently selected model, including its type.
    user_message, _, _ = await conversation_service.send_conversation_message(
        db, data.conversation.id, data.user, ConversationSendMessageRequest(content="再生成一段"),
    )
    assert user_message.message_type == "video"


@pytest.mark.asyncio
async def test_concurrent_mixed_messages_get_unique_sequence_numbers(
    text_conversation_db, mixed_conversation_models, monkeypatch,
):
    import asyncio

    monkeypatch.setattr(task_dispatch, "publish_task_message", lambda **kwargs: None)
    data = text_conversation_db
    conversation_id = data.conversation.id
    user_id = data.user.id
    model_ids = [model.id for model in mixed_conversation_models.values()]
    sessions = async_sessionmaker(data.session.bind, expire_on_commit=False)

    async def send(model_id):
        async with sessions() as db:
            user = await db.get(User, user_id)
            user_message, assistant, _ = await conversation_service.send_conversation_message(
                db, conversation_id, user,
                ConversationSendMessageRequest(content="并发发送", ai_model_id=model_id),
            )
            return user_message.sequence_no, assistant.sequence_no

    pairs = await asyncio.gather(*(send(model_id) for model_id in model_ids))
    assert sorted(pairs) == [(1, 2), (3, 4), (5, 6)]


@pytest.mark.asyncio
@pytest.mark.parametrize("model_type", ["image", "video"])
@pytest.mark.parametrize("content,expected_prompt", [
    ("让 @人物图 在 @场景图 中行走", "让 @图片1 在 @图片2 中行走"),
    ("参考这些图片的色调", "参考这些图片的色调"),
    ("参考@{场景图}的色调，保留其他图片的构图", "参考@图片2的色调，保留其他图片的构图"),
])
async def test_named_image_references_are_saved_and_compiled_for_generation(
    text_conversation_db, mixed_conversation_models, monkeypatch, model_type, content, expected_prompt,
):
    monkeypatch.setattr(task_dispatch, "publish_task_message", lambda **kwargs: None)
    data = text_conversation_db
    references = [
        {"name": "人物图", "url": "https://cdn.example/character.png"},
        {"name": "场景图", "url": "https://cdn.example/scene.png"},
    ]
    user_message, assistant, _ = await conversation_service.send_conversation_message(
        data.session, data.conversation.id, data.user,
        ConversationSendMessageRequest(
            content=content, ai_model_id=mixed_conversation_models[model_type].id,
            image_references=references,
        ),
    )
    await data.session.refresh(user_message)
    assert user_message.content == content
    assert user_message.extra["image_references"] == references
    task = await data.session.get(UserTaskRecord, UUID(assistant.extra["task_record_id"]))
    assert task.prompt == expected_prompt
    assert task.extra["user_message_extra"]["image_urls"] == [item["url"] for item in references]
    assert "image_references" not in task.extra["user_message_extra"]


@pytest.mark.asyncio
@pytest.mark.parametrize("model_type", ["image", "video"])
async def test_image_references_are_scoped_to_current_submission(
    text_conversation_db, mixed_conversation_models, monkeypatch, model_type,
):
    monkeypatch.setattr(task_dispatch, "publish_task_message", lambda **kwargs: None)
    data = text_conversation_db
    db = data.session
    conversation_id = data.conversation.id
    model_id = mixed_conversation_models[model_type].id

    async def send(content, references):
        user_message, assistant, _ = await conversation_service.send_conversation_message(
            db, conversation_id, data.user,
            ConversationSendMessageRequest(
                content=content, ai_model_id=model_id, image_references=references,
            ),
        )
        task = await db.get(UserTaskRecord, UUID(assistant.extra["task_record_id"]))
        return user_message, task

    first, _ = await send("参考@{图片1}", [
        {"name": "图片1", "url": "https://cdn.example/previous.png"},
    ])
    first_id = first.id
    _, current_task = await send("参考@{图片1}", [
        {"name": "图片1", "url": "https://cdn.example/current.png"},
    ])
    assert current_task.extra["user_message_extra"]["image_urls"] == [
        "https://cdn.example/current.png",
    ]
    _, no_images_task = await send("生成一座山", [])
    assert not no_images_task.extra["user_message_extra"].get("image_urls")

    counts = {
        model: await db.scalar(select(func.count()).select_from(model))
        for model in (ConversationMessage, UserTaskRecord, UserPointsTransaction, TaskDispatchOutbox)
    }
    with pytest.raises(AppException) as error:
        await send("继续修改@{图片1}", [])
    assert error.value.code == 40016
    await db.rollback()
    for model, count in counts.items():
        assert await db.scalar(select(func.count()).select_from(model)) == count
    # 历史引用只用于消息回显，不提供下一轮的候选图片或绑定。
    first = await db.get(ConversationMessage, first_id)
    assert first.extra["image_references"][0]["url"] == "https://cdn.example/previous.png"


@pytest.mark.asyncio
async def test_invalid_image_mentions_do_not_create_messages_tasks_or_charges(
    text_conversation_db, mixed_conversation_models,
):
    data = text_conversation_db
    with pytest.raises(AppException) as error:
        await conversation_service.send_conversation_message(
            data.session, data.conversation.id, data.user,
            ConversationSendMessageRequest(
                content="@没有绑定的图", ai_model_id=mixed_conversation_models["image"].id,
                image_references=[{
                    "name": "人物图", "url": "https://cdn.example/character.png",
                }],
            ),
        )
    assert error.value.code == 40016
    await data.session.rollback()
    for model in (ConversationMessage, UserTaskRecord, UserPointsTransaction, TaskDispatchOutbox):
        assert await data.session.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.asyncio
async def test_text_idempotency_does_not_silently_ignore_new_image_references(
    text_conversation_db, monkeypatch,
):
    monkeypatch.setattr(task_dispatch, "publish_task_message", lambda **kwargs: None)
    data = text_conversation_db
    payload = {"content": "@人物图", "client_message_id": "original-text"}
    await conversation_service.send_conversation_message(
        data.session, data.conversation.id, data.user, ConversationSendMessageRequest(**payload),
    )
    with pytest.raises(AppException) as error:
        await conversation_service.send_conversation_message(
            data.session, data.conversation.id, data.user,
            ConversationSendMessageRequest(**payload, image_references=[{
                "name": "人物图", "url": "https://cdn.example/character.png",
            }]),
        )
    assert error.value.code == 40997


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [
    {"generation_mode": "text_to_video", "media_items": [
        {"type": "video_url", "video_url": "https://cdn.example/reference.mp4"},
    ]},
    {"generation_mode": "reference", "image_urls": ["https://cdn.example/reference.png"],
     "video_urls": ["https://cdn.example/reference.mp4"]},
])
async def test_invalid_video_mode_media_is_rejected_before_task_creation(
    text_conversation_db, mixed_conversation_models, extra,
):
    data = text_conversation_db
    model = mixed_conversation_models["video"]
    model.vendor = "comfly"
    model.model_id = "grok-video-3"
    await data.session.commit()
    with pytest.raises(AppException) as error:
        await conversation_service.send_conversation_message(
            data.session, data.conversation.id, data.user,
            ConversationSendMessageRequest(content="生成视频", ai_model_id=model.id, extra=extra),
        )
    assert error.value.code == 40012
    await data.session.rollback()
    for table in (ConversationMessage, UserTaskRecord, UserPointsTransaction, TaskDispatchOutbox):
        assert await data.session.scalar(select(func.count()).select_from(table)) == 0


@pytest.mark.asyncio
async def test_frame_task_contains_only_frame_media_after_submission(
    text_conversation_db, mixed_conversation_models, monkeypatch,
):
    monkeypatch.setattr(task_dispatch, "publish_task_message", lambda **kwargs: None)
    data = text_conversation_db
    mixed_conversation_models["video"].model_id = "seedance-2.0"
    await data.session.commit()
    _, assistant, _ = await conversation_service.send_conversation_message(
        data.session, data.conversation.id, data.user,
        ConversationSendMessageRequest(
            content="首尾帧过渡", ai_model_id=mixed_conversation_models["video"].id,
            extra={
                "generation_mode": "first_last_frame",
                "firstFrameUrl": "https://cdn.example/start.png",
                "lastFrameUrl": "https://cdn.example/end.png",
                "video_urls": ["https://cdn.example/ignored.mp4"],
                "audio_url": "https://cdn.example/ignored.mp3",
            },
        ),
    )
    task = await data.session.get(UserTaskRecord, UUID(assistant.extra["task_record_id"]))
    model_extra = task.extra["user_message_extra"]
    assert model_extra["first_frame_url"] == "https://cdn.example/start.png"
    assert model_extra["last_frame_url"] == "https://cdn.example/end.png"
    assert "video_urls" not in model_extra and "audio_url" not in model_extra
