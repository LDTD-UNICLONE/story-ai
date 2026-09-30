"""Run PostgreSQL integration tests against an explicitly selected test database."""

import os
import argparse
from pathlib import Path
import sys


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.testing import validate_test_database_url

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--with-redis", action="store_true")
    options, pytest_args = parser.parse_known_args()
    try:
        validate_test_database_url(os.environ.get("TEST_DATABASE_URL", ""))
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    os.environ["RUN_DB_INTEGRATION_TESTS"] = "1"
    os.environ["RUN_REDIS_INTEGRATION_TESTS"] = "1" if options.with_redis else "0"

    import pytest

    return pytest.main(["-m", "integration", "-q", "-p", "no:cacheprovider", *pytest_args])


if __name__ == "__main__":
    raise SystemExit(main())
