import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from app.db.base import Base


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_DB_INTEGRATION_TESTS") != "1", reason="requires test PostgreSQL"
    ),
]


@pytest.fixture
def migration_db(test_database_url):
    schema = f"migration_{uuid4().hex}"
    engine = create_engine(
        make_url(test_database_url).set(drivername="postgresql+psycopg"), poolclass=NullPool
    )
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    try:
        with engine.connect() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.commit()
            config.attributes["connection"] = connection

            def migrate(revision="head", *, downgrade=False):
                connection.commit()
                (command.downgrade if downgrade else command.upgrade)(config, revision)
                connection.commit()

            yield SimpleNamespace(
                connection=connection, migrate=migrate, config=config, schema=schema
            )
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()


def test_canvas_upgrade_downgrade_preserves_projects(migration_db):
    fixture = migration_db
    fixture.migrate("0063_seedance_images")
    conn = fixture.connection
    uid, pid, cid, nid = uuid4(), uuid4(), uuid4(), uuid4()
    conn.execute(
        text("INSERT INTO users (id, account, password_hash, nickname) VALUES (:id, :account, 'test', 'Canvas')"),
        {"id": uid, "account": uuid4().hex},
    )
    conn.execute(
        text("INSERT INTO projects (id, user_id, name, cover, description) VALUES (:id, :uid, 'Preserved', '', '')"),
        {"id": pid, "uid": uid},
    )
    fixture.migrate()
    conn.execute(
        text("INSERT INTO project_canvases (id, project_id, name, viewport) VALUES (:id, :pid, 'Canvas', '{}')"),
        {"id": cid, "pid": pid},
    )
    conn.execute(
        text("INSERT INTO canvas_nodes (canvas_id, id, kind, title, x, y, width, height, content) VALUES (:cid, :id, 'text', '', 0, 0, 320, 240, '{}')"),
        {"cid": cid, "id": nid},
    )
    conn.commit()
    constraints = inspect(conn).get_unique_constraints("canvas_edges", schema=fixture.schema)
    assert any(c["name"] == "uq_canvas_edges_input_position" for c in constraints)
    fixture.migrate("0063_seedance_images", downgrade=True)
    assert not {"project_canvases", "canvas_nodes", "canvas_edges"} & set(
        inspect(conn).get_table_names(schema=fixture.schema)
    )
    assert conn.scalar(text("SELECT name FROM projects WHERE id=:id"), {"id": pid}) == "Preserved"
    fixture.migrate()
    assert conn.scalar(text("SELECT count(*) FROM project_canvases")) == 0


def test_empty_database_upgrades_all_tables_and_constraints(migration_db):
    fixture = migration_db
    fixture.migrate()
    connection = fixture.connection
    inspector = inspect(connection)
    assert set(inspector.get_table_names(schema=fixture.schema)) == set(Base.metadata.tables) | {
        "alembic_version"
    }
    assert (
        connection.scalar(text("SELECT version_num FROM alembic_version"))
        == ScriptDirectory.from_config(fixture.config).get_current_head()
    )
    for name, table in Base.metadata.tables.items():
        actual_columns = {
            column["name"]: column for column in inspector.get_columns(name, schema=fixture.schema)
        }
        assert set(actual_columns) == set(table.columns.keys()), name
        for column in table.columns:
            assert actual_columns[column.name]["nullable"] == column.nullable, (name, column.name)
        assert set(
            inspector.get_pk_constraint(name, schema=fixture.schema)["constrained_columns"]
        ) == {c.name for c in table.primary_key}
        actual_fks = {
            (tuple(fk["constrained_columns"]), fk["referred_table"], tuple(fk["referred_columns"]))
            for fk in inspector.get_foreign_keys(name, schema=fixture.schema)
        }
        expected_fks = {
            (
                tuple(c.name for c in fk.columns),
                fk.referred_table.name,
                tuple(e.column.name for e in fk.elements),
            )
            for fk in table.foreign_key_constraints
        }
        assert actual_fks == expected_fks, name


def test_canvas_generation_downgrade_refuses_to_relabel_video_as_image(migration_db):
    fixture = migration_db
    fixture.migrate()
    conn = fixture.connection
    uid, pid, mid = uuid4(), uuid4(), uuid4()
    conn.execute(text("INSERT INTO users (id, account, password_hash, nickname) VALUES (:id, :account, 'test', 'Canvas')"),
                 {"id": uid, "account": uuid4().hex})
    conn.execute(text("INSERT INTO projects (id, user_id, name, cover, description) VALUES (:id, :uid, 'Canvas', '', '')"),
                 {"id": pid, "uid": uid})
    conn.execute(text("INSERT INTO project_media (id, project_id, source_key, source_type, media_type, upload) VALUES (:id, :pid, 'canvas:video', 'canvas_generation', 'video', '{}')"),
                 {"id": mid, "pid": pid})
    conn.commit()
    with pytest.raises(RuntimeError, match="视频媒体"):
        fixture.migrate("0065_project_media", downgrade=True)
    conn.rollback()
    assert conn.scalar(text("SELECT version_num FROM alembic_version")) == ScriptDirectory.from_config(fixture.config).get_current_head()
    assert conn.scalar(text("SELECT media_type FROM project_media WHERE id=:id"), {"id": mid}) == "video"


def test_media_migration_preserves_canvas_and_downgrades_bindings(migration_db):
    fixture = migration_db
    fixture.migrate("0064_project_canvases")
    conn = fixture.connection
    uid, pid, cid, nid, mid = (uuid4() for _ in range(5))
    conn.execute(text("INSERT INTO users (id, account, password_hash, nickname) VALUES (:id, :account, 'test', 'Media')"),
                 {"id": uid, "account": uuid4().hex})
    conn.execute(text("INSERT INTO projects (id, user_id, name, cover, description) VALUES (:id, :uid, 'Media', '', '')"),
                 {"id": pid, "uid": uid})
    conn.execute(text("INSERT INTO project_canvases (id, project_id, name, viewport) VALUES (:id, :pid, 'Preserved', '{}')"),
                 {"id": cid, "pid": pid})
    conn.execute(text("INSERT INTO canvas_nodes (canvas_id, id, kind, title, x, y, width, height, content) VALUES (:cid, :id, 'image', '', 0, 0, 320, 240, '{}')"),
                 {"cid": cid, "id": nid})
    fixture.migrate()
    assert conn.scalar(text("SELECT media_id FROM canvas_nodes WHERE id=:id"), {"id": nid}) is None
    conn.execute(text("INSERT INTO project_media (id, project_id, source_key, source_type, upload) VALUES (:id, :pid, 'upload:test', 'upload', '{}')"),
                 {"id": mid, "pid": pid})
    conn.execute(text("UPDATE canvas_nodes SET media_id=:mid WHERE id=:nid"), {"mid": mid, "nid": nid})
    fixture.migrate("0064_project_canvases", downgrade=True)
    assert "project_media" not in inspect(conn).get_table_names(schema=fixture.schema)
    assert conn.scalar(text("SELECT name FROM project_canvases WHERE id=:id"), {"id": cid}) == "Preserved"
    assert conn.scalar(text("SELECT count(*) FROM canvas_nodes")) == 1
    fixture.migrate()
    assert conn.scalar(text("SELECT media_id FROM canvas_nodes WHERE id=:id"), {"id": nid}) is None


def test_existing_model_configuration_and_points_survive_upgrade(migration_db):
    fixture = migration_db
    fixture.migrate("0051_apimart_text_platform_rate")
    connection = fixture.connection
    user_id, model_id = uuid4(), uuid4()
    connection.execute(
        text(
            "INSERT INTO users (id, account, password_hash, nickname, points_balance) VALUES (:id, 'migration-user', 'test', 'Before upgrade', 91)"
        ),
        {"id": user_id},
    )
    connection.execute(
        text(
            "INSERT INTO user_points_transactions (user_id, amount, balance_after, transaction_type, remark) VALUES (:id, -9, 91, 'consume', 'before upgrade')"
        ),
        {"id": user_id},
    )
    connection.execute(
        text("""INSERT INTO ai_models (id, nickname, model_id, vendor, model_type,
        points_cost, model_multiplier, cache_multiplier, completion_multiplier, platform_multiplier,
        capabilities, billing_policy)
        VALUES (:id, 'Legacy model', 'migration-custom', 'comfly', 'text', 9, 2, 0.5, 3, 1.4,
        '{"supports_stream": true}', '{"type": "text"}')"""),
        {"id": model_id},
    )
    fixture.migrate()
    configuration = connection.scalar(
        text("SELECT configuration FROM ai_models WHERE id=:id"), {"id": model_id}
    )
    assert configuration["billing"]["base_points"] == 9
    assert float(configuration["billing"]["multipliers"]["model"]) == 2
    assert float(configuration["billing"]["multipliers"]["cache"]) == 0.5
    assert float(configuration["billing"]["multipliers"]["completion"]) == 3
    assert float(configuration["billing"]["multipliers"]["platform"]) == 1.4
    assert configuration["request"]["capabilities"]["supports_stream"] is True
    assert (
        connection.scalar(text("SELECT points_balance FROM users WHERE id=:id"), {"id": user_id})
        == 91
    )
    assert (
        connection.scalar(
            text("SELECT count(*) FROM user_points_transactions WHERE user_id=:id AND amount=-9"),
            {"id": user_id},
        )
        == 1
    )


def test_outbox_upgrade_downgrade_preserves_existing_business_data(migration_db):
    fixture = migration_db
    fixture.migrate("0061_remove_private_avatar_review")
    connection = fixture.connection
    connection.execute(
        text(
            "INSERT INTO users (account, password_hash, nickname, points_balance) VALUES ('outbox-upgrade', 'test', 'Migration', 123)"
        )
    )
    fixture.migrate()
    connection.execute(
        text(
            "INSERT INTO task_dispatch_outbox (task_name, args, queue) VALUES ('tasks.example.ping', '[]', 'story_ai_default')"
        )
    )
    assert connection.scalar(text("SELECT attempt_count FROM task_dispatch_outbox")) == 0
    fixture.migrate("0061_remove_private_avatar_review", downgrade=True)
    assert not inspect(connection).has_table("task_dispatch_outbox", schema=fixture.schema)
    fixture.migrate()
    assert connection.scalar(text("SELECT count(*) FROM task_dispatch_outbox")) == 0
    assert (
        connection.scalar(text("SELECT points_balance FROM users WHERE account='outbox-upgrade'"))
        == 123
    )
