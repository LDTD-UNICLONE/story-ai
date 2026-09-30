import os
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.testing import validate_test_database_url


@pytest.mark.parametrize(
    "url",
    [
        "",
        "not-a-url",
        "sqlite:///story_test",
        "postgresql://localhost/story_test",
        "postgresql+asyncpg://localhost/story_ai",
    ],
)
def test_database_runner_rejects_missing_or_non_test_connections(url):
    with pytest.raises(ValueError):
        validate_test_database_url(url)


def test_database_runner_preserves_explicit_test_credentials():
    url = "postgresql+asyncpg://tester:password%25with%40escapes@127.0.0.1:55443/story_ai_test"
    assert validate_test_database_url(url) == url


def test_collection_ignores_malformed_dotenv_and_inherited_app_settings(tmp_path):
    (tmp_path / ".env").write_text("POSTGRES_PORT=not-a-port\nREDIS_PORT=also-invalid\n")
    root = Path(__file__).resolve().parents[1]
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import os
from pathlib import Path
from scripts.testing import configure_test_settings
original_directory = Path.cwd()
settings = configure_test_settings()
assert Path.cwd() == original_directory
assert os.environ['POSTGRES_PORT'] == 'invalid-environment-port'
assert settings.postgres_port == 1
assert settings.redis_port == 1
assert settings.apimart_api_key == ''
assert settings.celery_broker_url == 'memory://'
assert settings.database_url.endswith('/unconfigured_test')
from app.main import app
from app.worker import celery_app
assert app.openapi()['paths']
assert 'tasks.task_dispatch.dispatch_pending_tasks' in celery_app.tasks
""",
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": str(root),
            "POSTGRES_PORT": "invalid-environment-port",
            "APIMART_API_KEY": "must-not-be-used",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert process.returncode == 0, process.stderr
