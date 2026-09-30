"""Best-effort invalidations only. Database snapshots recover missed notifications."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import settings


def task_phase(status, extra=None):
    if status == "success":
        return "completed"
    if status == "failed":
        return "failed"
    if (extra or {}).get("generation_phase") == "persisting":
        return "persisting"
    return "queued" if status == "pending" else "generating"


async def publish_task_change(record):
    try:
        async with Redis.from_url(
            settings.redis_url(), socket_connect_timeout=0.2, socket_timeout=0.2,
        ) as redis:
            await redis.publish(f"task-events:{record.user_id}", str(record.id))
    except (RedisError, OSError):
        pass  # Publishing failure must never undo committed generation results.


@asynccontextmanager
async def task_change_subscription(user_id):
    redis = Redis.from_url(settings.redis_url(), socket_connect_timeout=1, socket_timeout=1)
    pubsub = redis.pubsub()
    token = str(uuid4())
    key = f"task-streams:{{{user_id}}}"
    lease_ms = (settings.task_events_fallback_seconds * 3 + 10) * 1000
    try:
        subscription = None
        try:
            acquired = await redis.eval("""
local t = redis.call('TIME')
local now = t[1] * 1000 + math.floor(t[2] / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[1]) then return 0 end
redis.call('ZADD', KEYS[1], now + tonumber(ARGV[2]), ARGV[3])
redis.call('PEXPIRE', KEYS[1], ARGV[2])
return 1
""", 1, key, settings.task_event_stream_max_connections, lease_ms, token)
            if acquired:
                await pubsub.subscribe(f"task-events:{user_id}")
                await pubsub.get_message(timeout=1)
                subscription = SimpleNamespace(pubsub=pubsub, redis=redis, key=key, token=token, lease_ms=lease_ms)
        except (RedisError, OSError):
            pass
        yield subscription
    finally:
        try:
            await redis.zrem(key, token)
        except (RedisError, OSError):
            pass
        await pubsub.aclose()
        await redis.aclose()


async def wait_for_task_change(subscription, task_ids, *, timeout=None):
    timeout = timeout or settings.task_events_fallback_seconds
    if subscription is None:
        await asyncio.sleep(min(2, timeout))
        return True
    deadline = asyncio.get_running_loop().time() + timeout
    try:
        renewed = await subscription.redis.eval("""
if not redis.call('ZSCORE', KEYS[1], ARGV[1]) then return 0 end
local t = redis.call('TIME')
local now = t[1] * 1000 + math.floor(t[2] / 1000)
redis.call('ZADD', KEYS[1], now + tonumber(ARGV[2]), ARGV[1])
redis.call('PEXPIRE', KEYS[1], ARGV[2])
return 1
""", 1, subscription.key, subscription.token, subscription.lease_ms)
        if not renewed:
            return False
        while (remaining := deadline - asyncio.get_running_loop().time()) > 0:
            message = await subscription.pubsub.get_message(ignore_subscribe_messages=True, timeout=remaining)
            if message and str(message["data"].decode() if isinstance(message["data"], bytes) else message["data"]) in task_ids:
                return True
            if message is None:
                # get_message may return early for subscription acknowledgements.
                await asyncio.sleep(0.01)
    except (RedisError, OSError):
        return False
    return True


def sse_event(event, data):
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
