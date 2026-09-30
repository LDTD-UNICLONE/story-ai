import os

import pytest
from scripts.testing import configure_test_settings, validate_test_database_url


configure_test_settings(
    database_url=os.environ.get("TEST_DATABASE_URL")
    if os.getenv("RUN_DB_INTEGRATION_TESTS") == "1"
    else None,
)


@pytest.fixture
def test_database_url():
    try:
        return validate_test_database_url(os.environ.get("TEST_DATABASE_URL", ""))
    except ValueError as exc:
        pytest.fail(str(exc))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    setattr(item, f"rep_{report.when}", report)
