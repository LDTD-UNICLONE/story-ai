"""Real Redis concurrency/notification tests. Use a dedicated local instance."""
import asyncio
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from app.core.config import settings
from app.services.generation import provider_budget as budget, task_events

pytestmark = [pytest.mark.integration, pytest.mark.skipif(not os.getenv("TEST_REDIS_PORT"), reason="dedicated TEST_REDIS_PORT required")]


@pytest.fixture(autouse=True)
def isolated_redis(monkeypatch):
    port = int(os.environ["TEST_REDIS_PORT"])
    assert port != 6379, "Use a separate disposable Redis instance"
    monkeypatch.setattr(settings, "redis_host", "127.0.0.1")
    monkeypatch.setattr(settings, "redis_port", port)
    monkeypatch.setattr(settings, "redis_password", None)
    monkeypatch.setattr(settings, "redis_db", 0)


async def test_200_independent_clients_obey_account_rate_budget(monkeypatch):
    monkeypatch.setattr(settings, "provider_query_requests_per_second", 5)
    monkeypatch.setattr(settings, "provider_query_max_concurrency", 200)
    vendor = uuid4().hex
    async def request():
        try:
            async with budget.provider_query_slot(vendor):
                return 1
        except budget.ProviderQueryDeferred:
            return 0
    accepted = sum(await asyncio.gather(*(request() for _ in range(200))))
    assert accepted == 5
    await asyncio.sleep(1.05)
    assert await request() == 1


async def test_independent_connections_obey_shared_concurrency_and_release(monkeypatch):
    monkeypatch.setattr(settings, "provider_query_requests_per_second", 100)
    monkeypatch.setattr(settings, "provider_query_max_concurrency", 2)
    vendor = uuid4().hex
    async with budget.provider_query_slot(vendor), budget.provider_query_slot(vendor):
        with pytest.raises(budget.ProviderQueryDeferred):
            async with budget.provider_query_slot(vendor):
                pytest.fail("Exceeded account concurrency")
    async with budget.provider_query_slot(vendor):
        pass


async def test_abandoned_lease_expires_and_does_not_block_account(monkeypatch):
    monkeypatch.setattr(settings, "provider_query_max_concurrency", 1)
    vendor = uuid4().hex
    key = f"provider-query:{{{budget.account_scope(vendor)}}}:active"
    async with Redis.from_url(settings.redis_url()) as redis:
        await redis.zadd(key, {"crashed-worker": 0})
    async with budget.provider_query_slot(vendor):
        pass


async def test_retry_after_applies_to_other_workers_sharing_account():
    vendor = uuid4().hex
    error = RuntimeError("429")
    error.status_code = 429
    error.response = SimpleNamespace(headers={"Retry-After": "10"})
    with pytest.raises(RuntimeError):
        async with budget.provider_query_slot(vendor):
            raise error
    with pytest.raises(budget.ProviderQueryDeferred) as raised:
        async with budget.provider_query_slot(vendor):
            pytest.fail("Ignored account cooldown")
    assert 9 <= raised.value.delay <= 10


async def test_redis_outage_defers_instead_of_bypassing_budget(monkeypatch):
    monkeypatch.setattr(settings, "redis_port", 1)
    with pytest.raises(budget.ProviderQueryDeferred):
        async with budget.provider_query_slot(uuid4().hex):
            pytest.fail("Budget must fail closed")


async def test_committed_change_wakes_user_subscription_and_releases_connection_slot(monkeypatch):
    monkeypatch.setattr(settings, "task_event_stream_max_connections", 1)
    record = SimpleNamespace(user_id=uuid4(), id=uuid4())
    async with task_events.task_change_subscription(record.user_id) as subscription:
        assert subscription is not None
        async with task_events.task_change_subscription(record.user_id) as excess:
            assert excess is None
        waiter = asyncio.create_task(task_events.wait_for_task_change(subscription, {str(record.id)}, timeout=2))
        await task_events.publish_task_change(record)
        assert await asyncio.wait_for(waiter, 0.5) is True
    async with task_events.task_change_subscription(record.user_id) as replacement:
        assert replacement is not None


async def test_other_users_events_do_not_wake_subscription():
    user_id, task_id = uuid4(), uuid4()
    async with task_events.task_change_subscription(user_id) as subscription:
        waiter = asyncio.create_task(task_events.wait_for_task_change(subscription, {str(task_id)}, timeout=1))
        await task_events.publish_task_change(SimpleNamespace(user_id=uuid4(), id=task_id))
        done, _ = await asyncio.wait({waiter}, timeout=0.05)
        assert not done
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
