import asyncio
import os
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from app.api.deps import get_current_user
from app.api.v1.endpoints.project_canvases import router
from app.core.exceptions import AppException, register_exception_handlers
from app.db.session import get_db
from app.models.project import Project
from app.models.project_canvas import CanvasEdge, CanvasNode, ProjectCanvas
from app.models.task_record import UserTaskRecord
from app.models.points import UserPointsTransaction
from app.models.task_dispatch import TaskDispatchOutbox
from app.models.user import User
from app.schemas.project_canvas import CanvasCreate, CanvasNodeInput, CanvasEdgeInput, CanvasPatch
from app.services.projects import canvases
from tests.test_task_lifecycle_integration import lifecycle_db  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.skipif(os.getenv("RUN_DB_INTEGRATION_TESTS") != "1", reason="isolated PostgreSQL"),
]


@pytest.fixture
async def canvas_ctx(lifecycle_db):  # noqa: F811
    async with lifecycle_db() as db:
        user = User(account=uuid4().hex, password_hash="test", nickname="canvas", points_balance=90)
        db.add(user)
        await db.flush()
        project = Project(user_id=user.id, name="Canvas project", cover="", description="")
        db.add(project)
        await db.commit()
        uid, pid = user.id, project.id
        canvas = await canvases.create_canvas(db, pid, uid, CanvasCreate(name="场景一"))
        yield SimpleNamespace(db=db, factory=lifecycle_db, uid=uid, pid=pid, cid=canvas.id)


async def patch(ctx, revision=1, **values):
    return await canvases.patch_canvas(
        ctx.db, ctx.pid, ctx.uid, ctx.cid, CanvasPatch(expected_revision=revision, **values)
    )


def node(kind="image", **kwargs):
    return CanvasNodeInput(id=uuid4(), kind=kind, **kwargs)


def edge(source, target, **kwargs):
    return CanvasEdgeInput(id=uuid4(), source_id=source.id, target_id=target.id, **kwargs)


async def test_save_restore_versions_and_no_billing(canvas_ctx):
    ctx = canvas_ctx
    a, b = node(content={"text": "人物图"}), node("video")
    link = edge(a, b, input="first_frame")
    saved = await patch(
        ctx, upsert_nodes=[a, b], upsert_edges=[link], viewport={"x": 8, "zoom": 0.5}
    )
    assert saved.revision == 2
    await ctx.db.rollback()
    async with ctx.factory() as db:
        restored = await canvases.get_canvas(db, ctx.pid, ctx.uid, ctx.cid)
        assert restored.model_dump() == saved.model_dump()
    a.x = 100
    moved = await patch(ctx, 2, upsert_nodes=[a])
    assert all(n.content_revision == 1 for n in moved.nodes)
    a.content.text = "修改后的提示词"
    edited = await patch(ctx, 3, upsert_nodes=[a])
    assert {n.id: n.content_revision for n in edited.nodes} == {a.id: 2, b.id: 1}
    detached = await patch(ctx, 4, delete_edge_ids=[link.id])
    assert {n.id: n.content_revision for n in detached.nodes} == {a.id: 2, b.id: 2}
    for table in (UserTaskRecord, UserPointsTransaction, TaskDispatchOutbox):
        assert await ctx.db.scalar(select(func.count()).select_from(table)) == 0
    assert (await ctx.db.get(User, ctx.uid)).points_balance == 90


async def test_delete_group_keeps_children_and_delete_source_removes_edges(canvas_ctx):
    ctx = canvas_ctx
    group = node("group")
    a, b = node(parent_id=group.id), node("video")
    await patch(ctx, upsert_nodes=[a, b, group], upsert_edges=[edge(a, b)])
    result = await patch(ctx, 2, delete_node_ids=[group.id])
    assert len(result.nodes) == 2 and all(n.parent_id is None for n in result.nodes)
    result = await patch(ctx, 3, delete_node_ids=[a.id])
    assert not result.edges and result.nodes[0].id == b.id
    assert result.nodes[0].content_revision == 2
    await canvases.delete_canvas(ctx.db, ctx.pid, ctx.uid, ctx.cid, 4)
    for table in (ProjectCanvas, CanvasNode, CanvasEdge):
        assert await ctx.db.scalar(select(func.count()).select_from(table)) == 0


@pytest.mark.parametrize(
    "case",
    [
        "foreign_endpoint",
        "self",
        "cycle",
        "group",
        "bad_frame",
        "duplicate_slot",
        "duplicate_input",
        "kind_change",
    ],
)
async def test_invalid_batch_is_atomic(canvas_ctx, case):
    ctx = canvas_ctx
    a, b = node(), node("video")
    await patch(ctx, upsert_nodes=[a, b])
    invalid_edges = [edge(a, b)]
    replacement = a.model_copy(deep=True)
    replacement.content.text = "不能被保存"
    if case == "foreign_endpoint":
        invalid_edges[0].source_id = uuid4()
    elif case == "self":
        invalid_edges[0].target_id = a.id
    elif case == "cycle":
        invalid_edges.append(edge(b, a))
    elif case == "group":
        replacement.parent_id = b.id
    elif case == "bad_frame":
        invalid_edges = [edge(b, a, input="first_frame")]
    elif case == "duplicate_slot":
        c = node()
        invalid_edges.append(edge(c, b))
        await patch(ctx, 2, upsert_nodes=[c])
    elif case == "duplicate_input":
        invalid_edges.append(edge(a, b, position=1))
    else:
        replacement.kind = "text"
    rev = 3 if case == "duplicate_slot" else 2
    with pytest.raises(AppException) as error:
        await patch(ctx, rev, upsert_nodes=[replacement], upsert_edges=invalid_edges)
    assert error.value.code == 40070
    # Even committing after validation failure cannot persist part of the batch.
    await ctx.db.commit()
    restored = await canvases.get_canvas(ctx.db, ctx.pid, ctx.uid, ctx.cid)
    assert restored.revision == rev and not restored.edges
    assert next(n for n in restored.nodes if n.id == a.id).content.text == ""


async def test_input_order_swap_is_atomic(canvas_ctx):
    ctx = canvas_ctx
    a, b, target = node(), node(), node("video")
    e1, e2 = edge(a, target), edge(b, target, position=1)
    await patch(ctx, upsert_nodes=[a, b, target], upsert_edges=[e1, e2])
    e1.position, e2.position = 1, 0
    result = await patch(ctx, 2, upsert_edges=[e1, e2])
    assert [e.source_id for e in result.edges] == [b.id, a.id]
    assert next(n for n in result.nodes if n.id == target.id).content_revision == 2


async def test_concurrent_writers_only_one_succeeds(canvas_ctx):
    ctx = canvas_ctx

    async def write(name):
        async with ctx.factory() as db:
            try:
                result = await canvases.patch_canvas(
                    db, ctx.pid, ctx.uid, ctx.cid, CanvasPatch(expected_revision=1, name=name)
                )
                return result.revision
            except AppException as exc:
                return exc.code

    assert sorted(await asyncio.gather(write("A"), write("B"))) == [2, 40978]


async def test_reconnect_while_deleting_source_and_full_canvas_delete(canvas_ctx):
    ctx = canvas_ctx
    group = node("group")
    a, b, target = node(parent_id=group.id), node(), node("video")
    link = edge(a, target)
    await patch(ctx, upsert_nodes=[group, a, b, target], upsert_edges=[link])
    link.source_id = b.id
    result = await patch(ctx, 2, delete_node_ids=[a.id], upsert_edges=[link])
    assert result.edges[0].source_id == b.id
    assert next(n for n in result.nodes if n.id == target.id).content_revision == 2
    b.parent_id = group.id
    await patch(ctx, 3, upsert_nodes=[b])
    await canvases.delete_canvas(ctx.db, ctx.pid, ctx.uid, ctx.cid, 4)
    for table in (ProjectCanvas, CanvasNode, CanvasEdge):
        assert await ctx.db.scalar(select(func.count()).select_from(table)) == 0


@pytest.mark.parametrize("case", ["foreign_user", "other_project", "agent", "disabled"])
async def test_canvas_ownership_and_project_boundary(canvas_ctx, case):
    ctx = canvas_ctx
    pid, uid = ctx.pid, ctx.uid
    if case == "foreign_user":
        uid = uuid4()
    elif case == "other_project":
        other = Project(user_id=uid, name="Other", cover="", description="")
        ctx.db.add(other)
        await ctx.db.commit()
        pid = other.id
    else:
        project = await ctx.db.get(Project, pid)
        if case == "agent":
            project.project_kind = "agent"
        else:
            project.is_enabled = False
        await ctx.db.commit()
    for operation in (
        lambda: canvases.get_canvas(ctx.db, pid, uid, ctx.cid),
        lambda: canvases.patch_canvas(
            ctx.db, pid, uid, ctx.cid, CanvasPatch(expected_revision=1, name="attack")
        ),
        lambda: canvases.delete_canvas(ctx.db, pid, uid, ctx.cid, 1),
    ):
        with pytest.raises(AppException) as error:
            await operation()
        assert error.value.status_code == 404


async def test_node_ids_are_scoped_and_cross_canvas_links_rejected(canvas_ctx):
    ctx = canvas_ctx
    other = await canvases.create_canvas(ctx.db, ctx.pid, ctx.uid, CanvasCreate(name="Other"))
    a = node()
    await patch(ctx, upsert_nodes=[a])
    b = node("video")
    with pytest.raises(AppException):
        await canvases.patch_canvas(
            ctx.db,
            ctx.pid,
            ctx.uid,
            other.id,
            CanvasPatch(expected_revision=1, upsert_nodes=[b], upsert_edges=[edge(a, b)]),
        )
    await ctx.db.rollback()
    result = await canvases.patch_canvas(
        ctx.db, ctx.pid, ctx.uid, other.id, CanvasPatch(expected_revision=1, upsert_nodes=[a])
    )
    assert result.nodes[0].id == a.id


async def test_canvas_http_contract(canvas_ctx):
    ctx = canvas_ctx
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    register_exception_handlers(app)

    async def session():
        async with ctx.factory() as db:
            yield db

    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=ctx.uid)
    path = f"/api/v1/projects/{ctx.pid}/canvases"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(path, json={"name": "新画布"})
        assert created.status_code == 200
        cid = created.json()["data"]["id"]
        listed = (await client.get(path, params={"page_size": 1})).json()["data"]
        assert listed["total"] == 2 and len(listed["items"]) == 1
        assert "nodes" not in listed["items"][0]
        detail = f"{path}/{cid}"
        saved = await client.patch(
            detail, json={"expected_revision": 1, "upsert_nodes": [node().model_dump(mode="json")]}
        )
        assert saved.status_code == 200 and saved.json()["data"]["revision"] == 2
        assert (await client.get(detail)).json()["data"] == saved.json()["data"]
        stale = await client.patch(detail, json={"expected_revision": 1, "name": "旧版本"})
        assert stale.status_code == 409 and stale.json()["data"]["current_revision"] == 2
        assert (await client.delete(detail, params={"expected_revision": 1})).status_code == 409
        assert (
            await client.patch(detail, json={"expected_revision": 2, "viewport": None})
        ).status_code == 422
        assert (await client.delete(detail, params={"expected_revision": 2})).status_code == 200
        assert (await client.get(detail)).status_code == 404


async def test_import_records_are_owned_read_only_and_survive_node_edits(canvas_ctx):
    from app.api.v1.endpoints.canvas_import_records import router as records_router
    from app.models.canvas_import_record import CanvasImportRecord

    ctx = canvas_ctx
    n = node('text', content={'text': 'Migrated text'})
    await patch(ctx, upsert_nodes=[n])
    record = CanvasImportRecord(
        id=uuid4(), project_id=ctx.pid, source_type='chapter', source_id=uuid4(),
        data={'content': 'Original text', 'is_enabled': True},
        nodes=[{'canvas_id': str(ctx.cid), 'node_id': str(n.id)}], warnings=[],
    )
    ctx.db.add(record)
    await ctx.db.flush()
    stored = await ctx.db.get(CanvasNode, (ctx.cid, n.id))
    stored.import_record_id = record.id
    await ctx.db.commit()
    n.x = 25
    saved = await patch(ctx, 2, upsert_nodes=[n])
    assert saved.nodes[0].import_record_id == record.id
    app = FastAPI()
    app.include_router(records_router, prefix='/api/v1')
    register_exception_handlers(app)

    async def session():
        yield ctx.db

    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=ctx.uid)
    path = f'/api/v1/projects/{ctx.pid}/import-records'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        listing = (await client.get(path)).json()['data']
        assert listing['total'] == 1 and 'data' not in listing['items'][0]
        detail = (await client.get(f'{path}/{record.id}')).json()['data']
        assert detail['data']['content'] == 'Original text'
        assert (await client.patch(f'{path}/{record.id}', json={})).status_code == 405
        assert (await client.get(f'{path}/{uuid4()}')).status_code == 404
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=uuid4())
        assert (await client.get(path)).status_code == 404
        assert (await client.get(f'{path}/{record.id}')).status_code == 404
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=ctx.uid)
        await patch(ctx, 3, delete_node_ids=[n.id])
        assert (await client.get(f'{path}/{record.id}')).json()['data'] == detail
