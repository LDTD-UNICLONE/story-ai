import base64
import hashlib
import hmac
from datetime import timedelta
from typing import Any, Dict

import jwt
from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError

from app.core.config import settings
from app.core.timezone import beijing_datetime

password_context = PasswordHash.recommended()
LEGACY_PBKDF2_SHA256_PREFIX = "$pbkdf2-sha256$"
MAX_LEGACY_PBKDF2_ROUNDS = 1_000_000


def hash_password(password: str) -> str:
    return password_context.hash(password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    if password_hash.startswith(LEGACY_PBKDF2_SHA256_PREFIX):
        return _verify_legacy_pbkdf2_sha256(plain_password, password_hash)

    try:
        return password_context.verify(plain_password, password_hash)
    except (UnknownHashError, ValueError):
        return False


def verify_password_and_update(
    plain_password: str,
    password_hash: str,
) -> tuple[bool, str | None]:
    if password_hash.startswith(LEGACY_PBKDF2_SHA256_PREFIX):
        verified = _verify_legacy_pbkdf2_sha256(plain_password, password_hash)
        return verified, hash_password(plain_password) if verified else None

    try:
        return password_context.verify_and_update(plain_password, password_hash)
    except (UnknownHashError, ValueError):
        return False, None


def _verify_legacy_pbkdf2_sha256(plain_password: str, password_hash: str) -> bool:
    try:
        _, algorithm, rounds_value, salt_value, checksum = password_hash.split("$")
        if algorithm != "pbkdf2-sha256":
            return False
        rounds = int(rounds_value)
        if rounds <= 0 or rounds > MAX_LEGACY_PBKDF2_ROUNDS:
            return False
        salt = _decode_passlib_base64(salt_value)
        expected_checksum = _decode_passlib_base64(checksum)
    except (TypeError, ValueError):
        return False

    calculated_checksum = hashlib.pbkdf2_hmac(
        "sha256",
        plain_password.encode("utf-8"),
        salt,
        rounds,
    )
    return hmac.compare_digest(calculated_checksum, expected_checksum)


def _decode_passlib_base64(value: str) -> bytes:
    normalized = value.replace(".", "+")
    padding = "=" * (-len(normalized) % 4)
    return base64.b64decode(normalized + padding, validate=True)


def create_access_token(subject: str, *, token_version: int = 0) -> str:
    expires_at = beijing_datetime() + timedelta(minutes=settings.access_token_expire_minutes)
    payload: Dict[str, Any] = {"sub": subject, "ver": token_version, "exp": expires_at}
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> Dict[str, Any]:
    return jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
