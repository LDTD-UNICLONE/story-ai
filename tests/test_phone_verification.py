import pytest

from app.core.exceptions import AppException
from app.services import phone_verification


class FakeRedis:
    def __init__(self) -> None:
        self.values = {}

    async def get(self, key):
        return self.values.get(key)

    async def incr(self, key):
        value = int(self.values.get(key, 0)) + 1
        self.values[key] = value
        return value

    async def expire(self, key, seconds):
        return True

    async def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)


@pytest.mark.asyncio
async def test_sms_code_is_invalidated_after_too_many_attempts(monkeypatch) -> None:
    phone = "13800138000"
    redis = FakeRedis()
    redis.values[phone_verification._code_key(phone)] = "123456"
    monkeypatch.setattr(phone_verification.settings, "aliyun_sms_max_verify_attempts", 3)

    async def fake_redis():
        return redis

    monkeypatch.setattr(phone_verification, "_redis", fake_redis)

    for expected_status in (400, 400, 429):
        with pytest.raises(AppException) as exc_info:
            await phone_verification.assert_register_sms_code(phone, "000000")
        assert exc_info.value.status_code == expected_status

    assert phone_verification._code_key(phone) not in redis.values
