import pytest

from app.core import startup


@pytest.mark.parametrize(
    "setting_name",
    ["aliyun_sms_mock_enabled", "wechat_pay_mock_enabled"],
)
def test_non_local_environment_rejects_mock_integrations(monkeypatch, setting_name) -> None:
    monkeypatch.setattr(startup.settings, "app_env", "production")
    monkeypatch.setattr(startup.settings, "enforce_production_config", False)
    monkeypatch.setattr(startup.settings, "aliyun_sms_mock_enabled", False)
    monkeypatch.setattr(startup.settings, "wechat_pay_mock_enabled", False)
    monkeypatch.setattr(startup.settings, setting_name, True)

    with pytest.raises(RuntimeError, match="MOCK_ENABLED must be false"):
        startup.validate_runtime_config()


def test_local_environment_allows_mock_integrations(monkeypatch) -> None:
    monkeypatch.setattr(startup.settings, "app_env", "local")
    monkeypatch.setattr(startup.settings, "aliyun_sms_mock_enabled", True)
    monkeypatch.setattr(startup.settings, "wechat_pay_mock_enabled", True)

    startup.validate_runtime_config()


def test_non_local_environment_always_rejects_insecure_core_config(monkeypatch) -> None:
    monkeypatch.setattr(startup.settings, "app_env", "production")
    monkeypatch.setattr(startup.settings, "enforce_production_config", False)
    monkeypatch.setattr(startup.settings, "aliyun_sms_mock_enabled", False)
    monkeypatch.setattr(startup.settings, "wechat_pay_mock_enabled", False)
    monkeypatch.setattr(startup.settings, "app_debug", False)
    monkeypatch.setattr(startup.settings, "jwt_secret_key", "please-change-me-in-production")
    monkeypatch.setattr(startup.settings, "cors_origins", ["https://example.com"])

    with pytest.raises(RuntimeError, match="JWT_SECRET_KEY"):
        startup.validate_runtime_config()
