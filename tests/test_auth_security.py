from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.api import deps
from app.core.exceptions import AppException
from app.core.security import (
    create_access_token,
    hash_password,
    verify_password,
    verify_password_and_update,
)
from app.schemas.user import AdminPasswordResetRequest
from app.schemas.user import LoginRequest
from app.services import admin_users, auth

LEGACY_PASSWORD_HASH = (
    "$pbkdf2-sha256$29000$bGVnYWN5LXNhbHQ$"
    "WFzNACx8R1w7UdPfiyqzz5UCYlsR1RhHzg4DzhpiZCI"
)


def test_new_passwords_use_argon2() -> None:
    password_hash = hash_password("new-password")

    assert password_hash.startswith("$argon2")
    assert verify_password("new-password", password_hash)
    assert not verify_password("wrong-password", password_hash)


def test_legacy_password_is_verified_and_upgraded() -> None:
    verified, updated_hash = verify_password_and_update(
        "legacy-password",
        LEGACY_PASSWORD_HASH,
    )

    assert verified
    assert updated_hash is not None
    assert updated_hash.startswith("$argon2")
    assert verify_password("legacy-password", updated_hash)


def test_invalid_or_unknown_password_hash_is_rejected() -> None:
    assert verify_password_and_update("wrong-password", LEGACY_PASSWORD_HASH) == (False, None)
    assert verify_password_and_update("password", "unknown-hash") == (False, None)


def test_password_reset_requires_at_least_eight_characters() -> None:
    with pytest.raises(ValidationError):
        AdminPasswordResetRequest(password="short7")


@pytest.mark.asyncio
async def test_token_version_mismatch_requires_new_login(monkeypatch) -> None:
    user_id = uuid4()
    user = SimpleNamespace(id=user_id, is_enabled=True, token_version=2)

    async def fake_get_user_by_id(db, requested_user_id):
        assert requested_user_id == user_id
        return user

    monkeypatch.setattr(deps, "get_user_by_id", fake_get_user_by_id)
    token = create_access_token(str(user_id), token_version=1)
    request = SimpleNamespace(state=SimpleNamespace())

    with pytest.raises(AppException, match="重新登录"):
        await deps._authenticate_access_token(request, token, SimpleNamespace())


@pytest.mark.asyncio
async def test_admin_password_reset_increments_token_version(monkeypatch) -> None:
    user = SimpleNamespace(password_hash="old", token_version=3)

    async def fake_get_user_or_404(db, user_id):
        return user

    class FakeDb:
        async def commit(self):
            return None

        async def refresh(self, item):
            return None

    monkeypatch.setattr(admin_users, "get_user_or_404", fake_get_user_or_404)
    monkeypatch.setattr(admin_users, "hash_password", lambda value: f"hashed:{value}")

    await admin_users.reset_user_password(FakeDb(), uuid4(), "new-password")

    assert user.password_hash == "hashed:new-password"
    assert user.token_version == 4


@pytest.mark.asyncio
async def test_unknown_login_still_performs_password_verification(monkeypatch) -> None:
    async def fake_get_user_by_identity(db, identity):
        return None

    verified_hashes = []
    monkeypatch.setattr(auth, "get_user_by_identity", fake_get_user_by_identity)
    monkeypatch.setattr(
        auth,
        "verify_password",
        lambda password, password_hash: verified_hashes.append(password_hash) or False,
    )

    with pytest.raises(AppException, match="账号或密码错误"):
        await auth.login_user(
            SimpleNamespace(),
            LoginRequest(identifier="missing", password="any-password"),
        )

    assert verified_hashes == [auth.DUMMY_PASSWORD_HASH]


@pytest.mark.asyncio
async def test_login_upgrades_legacy_password_hash(monkeypatch) -> None:
    user = SimpleNamespace(
        id=uuid4(),
        account="legacy-user",
        password_hash=LEGACY_PASSWORD_HASH,
        nickname="Legacy User",
        avatar=None,
        phone=None,
        email=None,
        is_admin=False,
        is_enabled=True,
        points_balance=0,
        token_version=0,
    )

    async def fake_get_user_by_identity(db, identity):
        assert identity == "legacy-user"
        return user

    class FakeDb:
        commit_count = 0

        async def commit(self):
            self.commit_count += 1

    db = FakeDb()
    monkeypatch.setattr(auth, "get_user_by_identity", fake_get_user_by_identity)

    result = await auth.login_user(
        db,
        LoginRequest(identifier="legacy-user", password="legacy-password"),
    )

    assert result.user.id == user.id
    assert user.password_hash.startswith("$argon2")
    assert db.commit_count == 1
