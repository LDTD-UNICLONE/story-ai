from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from app.core import rate_limit
from app.core.exceptions import safe_validation_errors
from app.schemas.user import LoginRequest


def request(client_host: str, forwarded_for: str):
    return SimpleNamespace(
        state=SimpleNamespace(user_id=None),
        url=SimpleNamespace(path="/api/v1/auth/login"),
        headers={"x-forwarded-for": forwarded_for},
        client=SimpleNamespace(host=client_host),
    )


def test_untrusted_client_cannot_choose_rate_limit_identity(monkeypatch) -> None:
    monkeypatch.setattr(rate_limit.settings, "trusted_proxy_ips", ["127.0.0.1"])

    identity = rate_limit._identity(request("198.51.100.10", "1.2.3.4"))

    assert identity == "ip:198.51.100.10"


def test_trusted_proxy_can_forward_original_client_ip(monkeypatch) -> None:
    monkeypatch.setattr(rate_limit.settings, "trusted_proxy_ips", ["127.0.0.0/8"])

    identity = rate_limit._identity(request("127.0.0.1", "1.2.3.4, 127.0.0.1"))

    assert identity == "ip:1.2.3.4"


def test_trusted_proxy_ignores_attacker_supplied_leftmost_forward(monkeypatch) -> None:
    monkeypatch.setattr(rate_limit.settings, "trusted_proxy_ips", ["127.0.0.0/8"])

    identity = rate_limit._identity(
        request("127.0.0.1", "9.9.9.9, 1.2.3.4, 127.0.0.2")
    )

    assert identity == "ip:1.2.3.4"


def test_validation_errors_do_not_include_password_input() -> None:
    try:
        LoginRequest(identifier="demo", password="secret-value" * 20)
    except ValidationError as exc:
        request_error = RequestValidationError(exc.errors())
    else:
        raise AssertionError("expected validation error")

    errors = safe_validation_errors(request_error)

    assert errors
    assert "input" not in errors[0]
    assert "secret-value" not in str(errors)


def test_polling_identity_is_aggregated_across_resource_ids() -> None:
    first = request("198.51.100.10", "")
    first.url.path = "/api/v1/task-records/first"
    second = request("198.51.100.10", "")
    second.url.path = "/api/v1/task-records/second"

    assert rate_limit._identity(first) == rate_limit._identity(second)


def test_new_upload_and_generation_routes_use_specific_limits() -> None:
    upload_request = request("198.51.100.10", "")
    upload_request.method = "POST"
    upload_request.url.path = "/api/v1/agent-productions/from-file"
    generation_request = request("198.51.100.10", "")
    generation_request.method = "POST"
    generation_request.url.path = "/api/v1/agent-productions/id/storyboards/generations"

    assert rate_limit._match_rule(upload_request) == "upload"
    assert rate_limit._match_rule(generation_request) == "generation"


def test_rate_limiter_fails_closed_outside_local_environment(monkeypatch) -> None:
    protected_app = FastAPI()
    protected_app.add_middleware(rate_limit.RedisRateLimitMiddleware)

    @protected_app.get("/protected")
    async def protected() -> dict[str, bool]:
        return {"ok": True}

    monkeypatch.setattr(rate_limit.settings, "rate_limit_enabled", True)
    monkeypatch.setattr(rate_limit.settings, "app_env", "production")
    monkeypatch.setattr(rate_limit, "get_redis", lambda: None)

    with TestClient(protected_app) as client:
        response = client.get("/protected")

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
