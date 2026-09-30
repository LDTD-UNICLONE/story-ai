"""Test-only configuration shared by pytest and isolated Celery subprocesses."""

import os
from contextlib import chdir
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


_workspace = TemporaryDirectory(prefix="story-ai-tests-")
REDIS_VISIBILITY_TIMEOUT = 2


def validate_test_database_url(value: str) -> str:
    try:
        url = make_url(value)
    except ArgumentError:
        raise ValueError("Set TEST_DATABASE_URL to a PostgreSQL asyncpg test database") from None
    if url.drivername != "postgresql+asyncpg" or not (url.database or "").endswith("_test"):
        raise ValueError(
            "TEST_DATABASE_URL must use postgresql+asyncpg and a database ending in _test"
        )
    return url.render_as_string(hide_password=False)


def configure_test_settings(
    *,
    database_url: str | None = None,
    broker_url: str = "memory://",
    result_backend: str = "cache+memory://",
    log_dir: str | None = None,
):
    # Import config in an empty directory/environment so even a malformed developer
    # .env cannot affect collection. Restore both before importing application modules.
    with chdir(_workspace.name), patch.dict(os.environ, {}, clear=True):
        from app.core import config

        config.settings = config.Settings(
            _env_file=None,
            app_env="test",
            jwt_secret_key="story-ai-test-secret-with-at-least-32-characters",
            postgres_host="127.0.0.1",
            postgres_port=1,
            postgres_db="unconfigured_test",
            redis_host="127.0.0.1",
            redis_port=1,
            log_dir=log_dir or str(Path(_workspace.name) / "logs"),
        )
    settings = config.settings
    if database_url is not None:
        database_url = validate_test_database_url(database_url)
        settings.database_url = database_url
        settings.sync_database_url = (
            make_url(database_url)
            .set(drivername="postgresql+psycopg")
            .render_as_string(hide_password=False)
        )
        settings.rate_limit_enabled = False
    settings.celery_broker_url = broker_url
    settings.celery_result_backend = result_backend
    return settings
