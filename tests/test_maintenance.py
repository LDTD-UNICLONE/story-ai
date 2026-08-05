from types import SimpleNamespace

import pytest

from app import main


@pytest.mark.asyncio
async def test_maintenance_cleanup_runs_only_after_acquiring_lock(monkeypatch) -> None:
    calls = []

    class FakeConnection:
        def __init__(self) -> None:
            self.execute_count = 0
            self.commit_count = 0

        async def execute(self, _statement, _parameters):
            self.execute_count += 1
            return SimpleNamespace(scalar_one=lambda: True)

        async def commit(self):
            self.commit_count += 1

    connection = FakeConnection()

    class ConnectionContext:
        async def __aenter__(self):
            return connection

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return None

    class FakeSession:
        def __init__(self, *, bind, expire_on_commit):
            assert bind is connection
            assert expire_on_commit is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return None

    async def record(name, _db, *, limit):
        calls.append((name, limit))

    monkeypatch.setattr(
        main, "engine", SimpleNamespace(connect=lambda: ConnectionContext())
    )
    monkeypatch.setattr(main, "AsyncSession", FakeSession)
    monkeypatch.setattr(
        main,
        "purge_expired_pending_recharge_orders",
        lambda db, *, limit: record("recharges", db, limit=limit),
    )
    monkeypatch.setattr(
        main,
        "purge_unused_work_uploads",
        lambda db, *, limit: record("uploads", db, limit=limit),
    )
    monkeypatch.setattr(
        main,
        "process_oss_deletion_outbox",
        lambda db, *, limit: record("oss", db, limit=limit),
    )

    assert await main._run_maintenance_cleanup_once() is True
    assert calls == [("recharges", 200), ("uploads", 100), ("oss", 100)]
    assert connection.execute_count == 2
    assert connection.commit_count == 2


@pytest.mark.asyncio
async def test_maintenance_cleanup_skips_when_lock_is_held(monkeypatch) -> None:
    class FakeConnection:
        async def execute(self, _statement, _parameters):
            return SimpleNamespace(scalar_one=lambda: False)

        async def commit(self):
            return None

    class ConnectionContext:
        async def __aenter__(self):
            return FakeConnection()

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return None

    monkeypatch.setattr(
        main, "engine", SimpleNamespace(connect=lambda: ConnectionContext())
    )

    assert await main._run_maintenance_cleanup_once() is False
