from app.core.config import settings


def validate_runtime_config() -> None:
    if settings.app_env.lower() in {"local", "development", "dev", "test"}:
        return

    errors = []
    if settings.aliyun_sms_mock_enabled:
        errors.append("ALIYUN_SMS_MOCK_ENABLED must be false outside local environment")
    if settings.wechat_pay_mock_enabled:
        errors.append("WECHAT_PAY_MOCK_ENABLED must be false outside local environment")
    if settings.app_debug:
        errors.append("APP_DEBUG must be false outside local environment")
    if settings.jwt_secret_key == "please-change-me-in-production":
        errors.append("JWT_SECRET_KEY must be changed outside local environment")
    if not settings.cors_origins:
        errors.append("CORS_ORIGINS must be configured outside local environment")
    if errors:
        raise RuntimeError("; ".join(errors))
    if not settings.enforce_production_config:
        return

    if (
        not settings.comfly_api_key
        and not settings.volcengine_ark_api_key
        and not settings.apimart_api_key
    ):
        errors.append("At least one model provider API key must be configured")
    if (
        not settings.oss_bucket_name
        or not settings.oss_access_key_id
        or not settings.oss_access_key_secret
    ):
        errors.append("OSS config must be configured")
    sms_access_key_id = settings.aliyun_sms_access_key_id or settings.oss_access_key_id
    sms_access_key_secret = settings.aliyun_sms_access_key_secret or settings.oss_access_key_secret
    if not settings.aliyun_sms_mock_enabled and (
        not sms_access_key_id
        or not sms_access_key_secret
        or not settings.aliyun_sms_sign_name
        or not settings.aliyun_sms_register_template_code
    ):
        errors.append("Aliyun SMS config must be configured")
    if not settings.wechat_pay_mock_enabled and (
        not settings.wechat_pay_appid
        or not settings.wechat_pay_mchid
        or not settings.wechat_pay_api_v3_key
        or not settings.wechat_pay_merchant_serial_no
        or not settings.wechat_pay_private_key_path
        or not settings.wechat_pay_platform_cert_path
        or not settings.wechat_pay_notify_url
    ):
        errors.append("WeChat Pay config must be configured")
    if errors:
        raise RuntimeError("; ".join(errors))
