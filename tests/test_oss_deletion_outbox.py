from app.core.timezone import beijing_datetime
from app.models.oss_deletion import OssDeletionOutbox
from app.services import oss_deletions


class FakeScalars:
    def __init__(self, records) -> None:
        self.records = records

    def all(self):
        return self.records


class FakeResult:
    def __init__(self, records) -> None:
        self.records = records

    def scalars(self):
        return FakeScalars(self.records)


class FakeDb:
    def __init__(self, records=None) -> None:
        self.records = records or []
        self.statements = []
        self.deleted = []
        self.commits = 0

    async def execute(self, statement):
        self.statements.append(statement)
        return FakeResult(self.records)

    async def delete(self, record) -> None:
        self.deleted.append(record)

    async def commit(self) -> None:
        self.commits += 1


async def test_enqueue_deduplicates_object_keys() -> None:
    db = FakeDb()

    await oss_deletions.enqueue_oss_deletions(db, ["works/a.png", "works/a.png", ""])

    assert len(db.statements) == 1
    compiled = db.statements[0].compile()
    object_key_params = [
        value for key, value in compiled.params.items() if key.startswith("object_key")
    ]
    assert object_key_params == ["works/a.png"]
    assert "ON CONFLICT" in str(db.statements[0])


async def test_successful_deletion_removes_outbox_record(monkeypatch) -> None:
    record = OssDeletionOutbox(
        object_key="works/a.png",
        attempt_count=0,
        next_attempt_at=beijing_datetime(),
    )
    db = FakeDb([record])
    deleted_keys = []

    class FakeOssClient:
        def delete_object(self, object_key: str) -> None:
            deleted_keys.append(object_key)

    monkeypatch.setattr(oss_deletions, "OssClient", FakeOssClient)

    deleted = await oss_deletions.process_oss_deletion_outbox(db)

    assert deleted == 1
    assert deleted_keys == ["works/a.png"]
    assert db.deleted == [record]
    assert db.commits == 1


async def test_failed_deletion_is_retained_with_backoff(monkeypatch) -> None:
    now = beijing_datetime()
    record = OssDeletionOutbox(
        object_key="works/a.png",
        attempt_count=0,
        next_attempt_at=now,
    )
    db = FakeDb([record])

    class FakeOssClient:
        def delete_object(self, object_key: str) -> None:
            raise RuntimeError("temporary OSS failure")

    monkeypatch.setattr(oss_deletions, "OssClient", FakeOssClient)

    deleted = await oss_deletions.process_oss_deletion_outbox(db)

    assert deleted == 0
    assert db.deleted == []
    assert db.commits == 1
    assert record.attempt_count == 1
    assert record.next_attempt_at > now
    assert record.last_error == "temporary OSS failure"
