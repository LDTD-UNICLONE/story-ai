import re
import secrets
from hmac import compare_digest

from redis.asyncio import Redis

from app.core.config import settings
from app.core.exceptions import AppException
from app.integrations.aliyun_sms import aliyun_sms_client
from app.integrations.redis import get_redis, init_redis

MAINLAND_PHONE_PATTERN = re.compile(r"^1[3-9]\d{9}$")
REGISTER_CODE_KEY_PREFIX = "sms:register:code"
REGISTER_COOLDOWN_KEY_PREFIX = "sms:register:cooldown"


def validate_mainland_phone(phone: str) -> str:
    phone = phone.strip()
    if not MAINLAND_PHONE_PATTERN.fullmatch(phone):
        raise AppException("手机号格式不正确", code=42210, status_code=422)
    return phone


async def send_register_sms_code(phone: str) -> None:
    phone = validate_mainland_phone(phone)
    redis = await _redis()

    cooldown_key = _cooldown_key(phone)
    cooldown_seconds = max(1, settings.aliyun_sms_send_interval_seconds)
    acquired = await redis.set(cooldown_key, "1", ex=cooldown_seconds, nx=True)
    if not acquired:
        ttl = await redis.ttl(cooldown_key)
        retry_after = max(1, ttl)
        raise AppException(f"验证码发送过于频繁，请 {retry_after} 秒后再试", code=42910, status_code=429)

    code = _generate_code()
    try:
        await aliyun_sms_client.send_register_code(phone, code)
        await redis.set(_code_key(phone), code, ex=max(60, settings.aliyun_sms_code_ttl_seconds))
    except Exception:
        await redis.delete(cooldown_key)
        raise


async def assert_register_sms_code(phone: str, code: str) -> None:
    phone = validate_mainland_phone(phone)
    code = code.strip()
    redis = await _redis()
    cached_code = await redis.get(_code_key(phone))
    if not cached_code or not compare_digest(str(cached_code), code):
        raise AppException("手机验证码错误或已过期", code=40010, status_code=400)


async def consume_register_sms_code(phone: str) -> None:
    phone = validate_mainland_phone(phone)
    redis = await _redis()
    await redis.delete(_code_key(phone))


async def _redis() -> Redis:
    redis = get_redis()
    if redis is None:
        redis = await init_redis()
    return redis


def _generate_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _code_key(phone: str) -> str:
    return f"{REGISTER_CODE_KEY_PREFIX}:{phone}"


def _cooldown_key(phone: str) -> str:
    return f"{REGISTER_COOLDOWN_KEY_PREFIX}:{phone}"
