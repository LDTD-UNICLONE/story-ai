import os
from uuid import uuid4

import pytest
from sqlalchemy import MetaData, inspect, text

from tests.test_migrations_integration import migration_db  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.getenv('RUN_DB_INTEGRATION_TESTS') != '1', reason='isolated PostgreSQL',
)]


def test_text_migration_preserves_existing_edges_and_blocks_lossy_downgrade(migration_db):  # noqa: F811
    fixture = migration_db
    fixture.migrate('0067_canvas_only_projects')
    conn = fixture.connection
    metadata = MetaData()
    metadata.reflect(conn)

    def insert(table, **values):
        values.setdefault('id', uuid4())
        conn.execute(metadata.tables[table].insert().values(**values))
        return values['id']

    uid = insert('users', account=uuid4().hex, password_hash='test', nickname='Migration')
    pid = insert('projects', user_id=uid, name='Project', cover='', description='')
    cid = insert('project_canvases', project_id=pid, name='Canvas', viewport={})
    ids = [insert('canvas_nodes', canvas_id=cid, kind='text', title='', x=0, y=0,
                  width=320, height=240, content={'text': 'Source'}) for _ in range(2)]
    eid = insert('canvas_edges', canvas_id=cid, source_id=ids[0], target_id=ids[1],
                 input='reference', position=0)
    conn.commit()
    fixture.migrate()
    row = conn.execute(text('SELECT input,text_source,text_snapshot FROM canvas_edges WHERE id=:id'), {'id': eid}).one()
    assert tuple(row) == ('reference', None, None)
    conn.execute(text("UPDATE canvas_edges SET input='text', text_source='input', text_snapshot=:snapshot WHERE id=:id"),
                 {'id': eid, 'snapshot': '{"text":"Pinned","generation_id":null}'})
    conn.commit()
    with pytest.raises(RuntimeError, match='文本引用连接'):
        fixture.migrate('0067_canvas_only_projects', downgrade=True)
    conn.rollback()
    assert conn.scalar(text('SELECT version_num FROM alembic_version')) == '0068_canvas_text_references'
    assert conn.scalar(text('SELECT text_snapshot FROM canvas_edges WHERE id=:id'), {'id': eid})['text'] == 'Pinned'
    conn.execute(text("UPDATE canvas_edges SET input='reference', text_source=NULL, text_snapshot=NULL WHERE id=:id"), {'id': eid})
    conn.commit()
    fixture.migrate('0067_canvas_only_projects', downgrade=True)
    assert 'text_snapshot' not in {c['name'] for c in inspect(conn).get_columns('canvas_edges', schema=fixture.schema)}
    assert conn.scalar(text('SELECT count(*) FROM canvas_edges')) == 1
    fixture.migrate()
