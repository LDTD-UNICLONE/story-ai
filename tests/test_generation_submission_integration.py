# ruff: noqa: F811
import os
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import AppException
from app.models.conversation import Conversation, ConversationMessage
from app.models.points import UserPointsTransaction
from app.models.task_dispatch import TaskDispatchOutbox
from app.models.task_record import UserTaskRecord
from app.models.user import User
from app.schemas.conversation import ConversationSendMessageRequest
from app.services.conversation import service as conversations
from app.services.generation import submission, task_dispatch
from tests.test_text_conversation_integration import (  # noqa: F401
    mixed_conversation_models, text_conversation_db,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_DB_INTEGRATION_TESTS") != "1", reason="isolated PostgreSQL"),
]


async def counts(db):
    return [await db.scalar(select(func.count()).select_from(table)) for table in (
        ConversationMessage, UserTaskRecord, UserPointsTransaction, TaskDispatchOutbox,
    )]


@pytest.mark.parametrize("kind", ["image", "text", "retry"])
async def test_failed_outbox_registration_rolls_back_entire_conversation_submission(
    text_conversation_db, mixed_conversation_models, monkeypatch, kind,
):
    ctx = text_conversation_db
    db = ctx.session
    uid, cid = ctx.user.id, ctx.conversation.id
    published = []
    monkeypatch.setattr(task_dispatch, "publish_task_message", lambda **kw: published.append(kw))
    if kind == "retry":
        user_message, assistant, _ = await conversations.send_conversation_message(
            db, cid, ctx.user, ConversationSendMessageRequest(content="initial"),
        )
        user_message_id = user_message.id
        task = await db.get(UserTaskRecord, UUID(assistant.extra["task_record_id"]))
        task.status = assistant.status = "failed"
        assistant.extra = {**assistant.extra, "task_status": "failed"}
        await db.commit()
    baseline = await counts(db)
    balance = ctx.user.points_balance
    default_model = ctx.conversation.ai_model_id
    updated_at = await db.scalar(select(Conversation.updated_at).where(Conversation.id == cid))
    published.clear()
    original = submission.enqueue_task_dispatch

    async def fail_after_insert(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("outbox registration failed")

    monkeypatch.setattr(submission, "enqueue_task_dispatch", fail_after_insert)
    with pytest.raises(RuntimeError, match="outbox registration failed"):
        if kind == "retry":
            await conversations.retry_text_conversation_turn(db, cid, user_message_id, ctx.user)
        else:
            await conversations.send_conversation_message(
                db, cid, ctx.user, ConversationSendMessageRequest(
                    content="failed submission", ai_model_id=mixed_conversation_models[kind].id,
                ),
            )
    # Committing after a caught error must not preserve a charge or orphan rows.
    await db.commit()
    assert await counts(db) == baseline
    assert await db.scalar(select(User.points_balance).where(User.id == uid)) == balance
    saved = await db.get(Conversation, cid)
    assert saved.ai_model_id == default_model and saved.conversation_type == "text"
    assert saved.updated_at == updated_at
    assert published == []


async def test_task_limit_after_charge_keeps_existing_submission_only(
    text_conversation_db, mixed_conversation_models, monkeypatch,
):
    ctx = text_conversation_db
    uid, cid = ctx.user.id, ctx.conversation.id
    model_id = mixed_conversation_models["image"].id
    monkeypatch.setattr(task_dispatch, "publish_task_message", lambda **kwargs: None)
    monkeypatch.setattr(settings, "user_pending_task_limit", 1)
    await conversations.send_conversation_message(
        ctx.session, cid, ctx.user,
        ConversationSendMessageRequest(content="first", ai_model_id=model_id),
    )
    baseline, balance = await counts(ctx.session), ctx.user.points_balance
    with pytest.raises(AppException) as error:
        await conversations.send_conversation_message(
            ctx.session, cid, ctx.user,
            ConversationSendMessageRequest(content="second", ai_model_id=model_id),
        )
    assert error.value.code == 42920
    await ctx.session.commit()
    assert await counts(ctx.session) == baseline
    assert await ctx.session.scalar(select(User.points_balance).where(User.id == uid)) == balance


@pytest.mark.parametrize("cost", [0, 7])
async def test_common_submission_does_not_commit_or_publish(text_conversation_db, monkeypatch, cost):
    ctx = text_conversation_db
    db = ctx.session
    uid, cid = ctx.user.id, ctx.conversation.id
    balance = ctx.user.points_balance

    def unexpected_publish(**kwargs):
        pytest.fail("Submission must not publish before the caller commits")

    monkeypatch.setattr(task_dispatch, "publish_task_message", unexpected_publish)
    async with submission.pending_generation(
        db, user_id=uid, ai_model_id=ctx.model.id,
        business_type="conversation", business_id=cid, generation_type="text",
        title="test", prompt="test", points_cost=cost, charge_remark="test charge",
        extra={}, task_name="tasks.model_generation.run_conversation_generation",
        task_args=(str(cid),),
    ) as task:
        ctx.conversation.title = "uncommitted business change"
    assert task.status == "pending" and task.points_cost == cost
    assert (task.points_transaction_id is not None) == bool(cost)
    outbox = await db.get(TaskDispatchOutbox, task.id)
    assert outbox.args == [str(task.id), str(cid)] and outbox.queue == "story_ai_text"
    async with AsyncSession(bind=db.bind) as observer:
        assert await counts(observer) == [0, 0, 0, 0]
        assert await observer.scalar(select(User.points_balance).where(User.id == uid)) == balance
    await db.rollback()
    assert await counts(db) == [0, 0, 0, 0]
    assert await db.scalar(select(User.points_balance).where(User.id == uid)) == balance
    assert (await db.get(Conversation, cid)).title == "多轮文本测试"
