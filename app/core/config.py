from functools import cached_property
from typing import List, Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Story AI Backend"
    app_env: str = "local"
    app_debug: bool = True
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    api_prefix: str = "/api/v1"
    cors_origins: List[str] = Field(default_factory=list)

    timezone: str = "Asia/Shanghai"
    jwt_secret_key: str = "please-change-me-in-production"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24 * 7
    default_user_avatar: str = "https://wpimg.wallstcn.com/f778738c-e4f8-4870-b634-56703b4acafe.gif"
    enforce_production_config: bool = True
    log_dir: str = "logs"
    log_file: str = "story-ai.log"
    log_access_file: str = "access.log"
    log_error_file: str = "error.log"
    log_level: str = "INFO"
    log_max_bytes: int = 10 * 1024 * 1024
    log_backup_count: int = 5
    log_sql_enabled: bool = False

    comfly_base_url: str = ""
    comfly_api_key: str = ""
    comfly_timeout_seconds: int = 30
    comfly_max_connections: int = 100
    comfly_max_keepalive_connections: int = 20

    volcengine_ark_base_url: str = "https://ark.cn-beijing.volces.com"
    volcengine_ark_api_key: str = ""
    volcengine_ark_timeout_seconds: int = 30
    volcengine_ark_max_connections: int = 100
    volcengine_ark_max_keepalive_connections: int = 20

    postgres_host: str = "127.0.0.1"
    postgres_port: int = 5432
    postgres_user: str = "postgres"
    postgres_password: str = "postgres"
    postgres_db: str = "story_ai"
    postgres_pool_size: int = 10
    postgres_max_overflow: int = 20
    postgres_pool_timeout: int = 30
    postgres_pool_recycle: int = 1800

    redis_host: str = "127.0.0.1"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: Optional[str] = None

    celery_broker_db: int = 1
    celery_result_db: int = 2
    celery_task_time_limit_seconds: int = 300
    celery_task_soft_time_limit_seconds: int = 240
    celery_task_max_retries: int = 5
    celery_task_retry_countdown_seconds: int = 30
    celery_task_retry_backoff_max_seconds: int = 300
    celery_result_expires_seconds: int = 60 * 60 * 24
    celery_worker_max_tasks_per_child: int = 50
    provider_task_poll_interval_seconds: int = 10
    provider_task_poll_max_attempts: int = 60
    provider_task_worker_poll_interval_seconds: int = 5
    provider_task_worker_poll_max_attempts: int = 3
    user_pending_task_limit: int = 20
    user_pending_media_task_limit: int = 5
    user_pending_task_window_hours: int = 24
    task_stale_timeout_minutes: int = 30
    generated_media_download_max_size_mb: int = 500
    generated_media_transfer_concurrency: int = 3
    generated_media_connect_timeout_seconds: int = 30
    generated_media_read_timeout_seconds: int = 300
    generated_media_upload_timeout_seconds: int = 120
    oss_endpoint: str = "https://oss-cn-hangzhou.aliyuncs.com"
    oss_bucket_name: str = ""
    oss_access_key_id: str = ""
    oss_access_key_secret: str = ""
    oss_public_base_url: str = ""
    oss_root_directory: str = "story"
    max_upload_size_mb: int = 100

    aliyun_sms_access_key_id: str = ""
    aliyun_sms_access_key_secret: str = ""
    aliyun_sms_region_id: str = "cn-hangzhou"
    aliyun_sms_sign_name: str = ""
    aliyun_sms_register_template_code: str = ""
    aliyun_sms_register_template_param_key: str = "code"
    aliyun_sms_code_ttl_seconds: int = 300
    aliyun_sms_send_interval_seconds: int = 60
    aliyun_sms_mock_enabled: bool = False

    wechat_pay_appid: str = ""
    wechat_pay_mchid: str = ""
    wechat_pay_api_v3_key: str = ""
    wechat_pay_merchant_serial_no: str = ""
    wechat_pay_merchant_cert_path: str = ""
    wechat_pay_private_key_path: str = ""
    wechat_pay_platform_cert_path: str = ""
    wechat_pay_notify_url: str = ""
    wechat_pay_timeout_seconds: int = 30
    wechat_pay_native_expire_minutes: int = 30
    wechat_pay_mock_enabled: bool = False

    rate_limit_enabled: bool = True
    rate_limit_window_seconds: int = 60
    rate_limit_global_requests: int = 300
    rate_limit_auth_requests: int = 30
    rate_limit_upload_requests: int = 30
    rate_limit_generation_requests: int = 60
    rate_limit_polling_requests: int = 300
    rate_limit_polling_window_seconds: int = 5

    @cached_property
    def database_url(self) -> str:
        return (
            "postgresql+asyncpg://"
            f"{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @cached_property
    def sync_database_url(self) -> str:
        return (
            "postgresql+psycopg://"
            f"{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    def redis_url(self, db: Optional[int] = None) -> str:
        selected_db = self.redis_db if db is None else db
        auth = f":{self.redis_password}@" if self.redis_password else ""
        return f"redis://{auth}{self.redis_host}:{self.redis_port}/{selected_db}"

    @cached_property
    def celery_broker_url(self) -> str:
        return self.redis_url(self.celery_broker_db)

    @cached_property
    def celery_result_backend(self) -> str:
        return self.redis_url(self.celery_result_db)


settings = Settings()
