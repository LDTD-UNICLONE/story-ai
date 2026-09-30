"""Account-wide query budgets shared across processes; Redis failure defers work."""

import hashlib
import math
import random
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from uuid import uuid4

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import settings

_ACQUIRE = """
local t = redis.call('TIME')
local now = t[1] * 1000 + math.floor(t[2] / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now - 1000)
local cooldown = redis.call('PTTL', KEYS[3])
if cooldown > 0 then return cooldown end
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[1]) then return 1000 end
if redis.call('ZCARD', KEYS[2]) >= tonumber(ARGV[2]) then return 1000 end
redis.call('ZADD', KEYS[1], now + tonumber(ARGV[3]), ARGV[4])
redis.call('PEXPIRE', KEYS[1], ARGV[3])
redis.call('ZADD', KEYS[2], now, ARGV[4])
redis.call('PEXPIRE', KEYS[2], 2000)
return 0
"""
_COOLDOWN = """
if redis.call('PTTL', KEYS[1]) < tonumber(ARGV[1]) then
  redis.call('PSETEX', KEYS[1], ARGV[1], '1')
end
return 1
"""


class ProviderQueryDeferred(Exception):
    def __init__(self, delay: int):
        self.delay = delay
        super().__init__("Provider query capacity unavailable")


def account_scope(vendor: str) -> str:
    vendor = "comfly" if vendor == "模型服务" else vendor
    base = getattr(settings, f"{vendor}_base_url", "")
    credential = getattr(settings, f"{vendor}_api_key", "")
    return hashlib.sha256(f"{vendor}|{base}|{credential}".encode()).hexdigest()


def retry_after_seconds(error) -> int:
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        response = getattr(error, "response", None)
        headers = getattr(response, "headers", {}) or {}
        raw = headers.get("Retry-After") or headers.get("retry-after")
        if raw:
            try:
                return max(0, math.ceil(float(raw)))
            except (ValueError, TypeError, OverflowError):
                try:
                    date = parsedate_to_datetime(str(raw))
                    return max(0, math.ceil((date - datetime.now(timezone.utc)).total_seconds()))
                except (ValueError, TypeError, OverflowError):
                    pass
        error = error.__cause__ or error.__context__
    return 0


def retry_delay(attempts: int, error=None) -> int:
    # Jitter only applies to failures, never to normal completion notifications.
    cap = settings.provider_query_retry_max_seconds
    backoff = min(cap, 2 ** min(max(attempts, 1), 10))
    return max(retry_after_seconds(error), min(cap, backoff + random.randint(0, 2)))


@asynccontextmanager
async def provider_query_slot(vendor: str):
    scope = account_scope(vendor)
    keys = [f"provider-query:{{{scope}}}:{suffix}" for suffix in ("active", "rate", "cooldown")]
    token = str(uuid4())
    async with Redis.from_url(
        settings.redis_url(), socket_connect_timeout=1, socket_timeout=1,
    ) as redis:
        try:
            delay_ms = await redis.eval(
                _ACQUIRE, 3, *keys, settings.provider_query_max_concurrency,
                settings.provider_query_requests_per_second,
                (settings.provider_query_timeout_seconds + 30) * 1000, token,
            )
        except RedisError as exc:
            raise ProviderQueryDeferred(5) from exc
        if delay_ms:
            raise ProviderQueryDeferred(max(1, math.ceil(delay_ms / 1000)))
        try:
            yield
        except Exception as exc:
            delay = retry_after_seconds(exc)
            error = exc
            seen = set()
            while error is not None and id(error) not in seen:
                seen.add(id(error))
                if getattr(error, "status_code", None) == 429 or getattr(getattr(error, "response", None), "status_code", None) == 429:
                    delay = max(delay, 5)
                error = error.__cause__ or error.__context__
            if delay:
                try:
                    await redis.eval(_COOLDOWN, 1, keys[2], delay * 1000)
                except RedisError:
                    pass
            raise
        finally:
            try:
                await redis.zrem(keys[0], token)
            except RedisError:
                pass  # Worker loss / Redis outage is covered by the expiring lease.
