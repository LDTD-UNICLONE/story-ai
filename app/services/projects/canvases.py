"""Atomic canvas editing; no generation, billing or external media operations."""

from collections import defaultdict, deque
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException
from app.core.timezone import beijing_datetime
from app.models.project_canvas import CanvasEdge, CanvasNode, ProjectCanvas
from app.models.project_media import ProjectMedia
from app.schemas.project_canvas import (
    CanvasCreate,
    CanvasLayoutPatch,
    CanvasContent,
    CanvasEdgeOut,
    CanvasNodeInput,
    CanvasNodeOut,
    CanvasOut,
    CanvasPatch,
    CanvasSummary,
)
from app.services.projects.queries import get_project_or_404
from app.services.projects.media import media_outputs
from app.services.projects.canvas_text_references import bind_text, selected_results, source_snapshot


def _invalid(message):
    raise AppException(message, code=40070, status_code=400)


def _check_revision(canvas, expected_revision):
    if canvas.revision != expected_revision:
        raise AppException(
            "画布已更新，请重新读取后合并修改",
            code=40978,
            status_code=409,
            data={"current_revision": canvas.revision},
        )


async def _canvas(db, project_id, user_id, canvas_id, *, lock=False):
    await get_project_or_404(db, project_id, user_id)
    query = (
        select(ProjectCanvas)
        .where(ProjectCanvas.id == canvas_id, ProjectCanvas.project_id == project_id)
        .execution_options(populate_existing=True)
    )
    if lock:
        query = query.with_for_update()
    canvas = await db.scalar(query)
    if canvas is None:
        raise AppException("画布不存在", code=40470, status_code=404)
    return canvas


async def _graph(db, canvas_id):
    nodes = list(
        await db.scalars(
            select(CanvasNode)
            .where(CanvasNode.canvas_id == canvas_id)
            .execution_options(populate_existing=True)
        )
    )
    edges = list(
        await db.scalars(
            select(CanvasEdge)
            .where(CanvasEdge.canvas_id == canvas_id)
            .execution_options(populate_existing=True)
        )
    )
    return {n.id: n for n in nodes}, {e.id: e for e in edges}


def _output(canvas, nodes, edges):
    return CanvasOut(
        **CanvasSummary.model_validate(canvas).model_dump(),
        nodes=[
            CanvasNodeOut.model_validate(n) for n in sorted(nodes.values(), key=lambda n: str(n.id))
        ],
        edges=[
            CanvasEdgeOut.model_validate(e)
            for e in sorted(
                edges.values(), key=lambda e: (str(e.target_id), e.input, e.position, str(e.id))
            )
        ],
    )


async def create_canvas(db: AsyncSession, project_id: UUID, user_id: UUID, payload: CanvasCreate):
    await get_project_or_404(db, project_id, user_id)
    canvas = ProjectCanvas(project_id=project_id, name=payload.name)
    db.add(canvas)
    await db.flush()
    result = _output(canvas, {}, {})
    await db.commit()
    return result


async def list_canvases(db, project_id, user_id, page, page_size):
    await get_project_or_404(db, project_id, user_id)
    condition = ProjectCanvas.project_id == project_id
    total = await db.scalar(select(func.count()).select_from(ProjectCanvas).where(condition))
    items = await db.scalars(
        select(ProjectCanvas)
        .where(condition)
        .order_by(ProjectCanvas.created_at.desc(), ProjectCanvas.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return dict(
        items=[CanvasSummary.model_validate(c).model_dump(mode="json") for c in items],
        total=total,
        page=page,
        page_size=page_size,
    )


async def get_canvas(db, project_id, user_id, canvas_id):
    # Hold the same lock as writers while reading the graph to avoid a mixed revision.
    canvas = await _canvas(db, project_id, user_id, canvas_id, lock=True)
    nodes, edges = await _graph(db, canvas_id)
    return _output(canvas, nodes, edges)


def _check_acyclic(ids, links):
    outgoing = defaultdict(list)
    degree = dict.fromkeys(ids, 0)
    for source, target in links:
        outgoing[source].append(target)
        degree[target] += 1
    ready = deque(node_id for node_id, count in degree.items() if count == 0)
    visited = 0
    while ready:
        source = ready.popleft()
        visited += 1
        for target in outgoing[source]:
            degree[target] -= 1
            if degree[target] == 0:
                ready.append(target)
    if visited != len(ids):
        _invalid("画布不允许循环连接或循环分组")


def _validate_graph(nodes, edges):
    if len(nodes) > 1000 or len(edges) > 3000:
        _invalid("单画布最多支持 1000 个节点、3000 条连接")
    parents = []
    for node in nodes.values():
        if node.parent_id is not None:
            parent = nodes.get(node.parent_id)
            if parent is None or parent.kind != "group":
                _invalid("父节点必须是当前画布中的分组")
            parents.append((node.parent_id, node.id))
    _check_acyclic(nodes, parents)
    slots = set()
    references = set()
    for edge in edges.values():
        source, target = nodes.get(edge.source_id), nodes.get(edge.target_id)
        if source is None or target is None or source.kind == "group" or target.kind == "group":
            _invalid("连接两端必须是当前画布中存在的非分组节点")
        if source.id == target.id:
            _invalid("节点不能连接自身")
        if edge.input == "text":
            if source.kind != "text":
                _invalid("文本输入连接的来源必须是文本节点")
        elif edge.input != "reference":
            if source.kind != "image" or target.kind != "video" or edge.position != 0:
                _invalid("首尾帧只支持图像连接视频，position 必须为 0")
        slot = (edge.target_id, edge.input, edge.position)
        reference = (edge.source_id, edge.target_id, edge.input)
        if slot in slots or reference in references:
            _invalid("输入连接或输入位置不能重复")
        slots.add(slot)
        references.add(reference)
    _check_acyclic(nodes, ((e.source_id, e.target_id) for e in edges.values()))


def _inputs(edges):
    result = defaultdict(set)
    for edge in edges.values():
        result[edge.target_id].add((edge.id, edge.source_id, edge.input, edge.position, edge.media_id,
                                   edge.text_source,
                                   CanvasEdgeOut.model_validate(edge).model_dump_json(include={"text_snapshot"})))
    return result


async def _bind_media(db, project_id, nodes, edges, old_edges, changed_edges):
    for node in nodes.values():
        if node.media_id is not None and node.kind not in {"image", "video"}:
            _invalid("仅图像或视频节点可绑定媒体素材")
    for incoming in changed_edges:
        edge = edges[incoming.id]
        source = nodes[edge.source_id]
        previous = old_edges.get(edge.id)
        same_source = previous is not None and previous.source_id == edge.source_id
        if source.kind != "image":
            if edge.media_id is not None:
                _invalid("仅图像输入连接可以固定图片素材")
            continue
        if "media_id" not in incoming.model_fields_set and same_source:
            edge.media_id = previous.media_id
        elif edge.media_id is None:
            edge.media_id = source.media_id
        elif edge.media_id != source.media_id and not (
            same_source and edge.media_id == previous.media_id
        ):
            _invalid("连接只能选择源节点当前图片或保留原固定版本")
    ids = {obj.media_id for obj in [*nodes.values(), *edges.values()] if obj.media_id is not None}
    if ids:
        owned = {m.id: m for m in await db.scalars(
            select(ProjectMedia).where(
                ProjectMedia.project_id == project_id, ProjectMedia.id.in_(ids)
            )
        )}
        if set(owned) != ids:
            _invalid("图片素材不存在或不属于当前项目")
        if any(n.media_id and owned[n.media_id].media_type != n.kind for n in nodes.values()):
            _invalid("节点类型与媒体类型不匹配")
        if any(e.media_id and owned[e.media_id].media_type != "image" for e in edges.values()):
            _invalid("当前输入连接只支持固定图片")


async def image_inputs(db, project_id, user_id, canvas_id, node_id):
    canvas = await _canvas(db, project_id, user_id, canvas_id, lock=True)
    target = await db.scalar(
        select(CanvasNode).where(CanvasNode.canvas_id == canvas_id, CanvasNode.id == node_id)
        .execution_options(populate_existing=True)
    )
    if target is None:
        raise AppException("节点不存在", code=40472, status_code=404)
    # Load this node's inputs, not every node's text and every edge in the canvas.
    pairs = list(await db.execute(
        select(CanvasEdge, CanvasNode).join(
            CanvasNode,
            (CanvasNode.canvas_id == CanvasEdge.canvas_id) & (CanvasNode.id == CanvasEdge.source_id),
        ).where(
            CanvasEdge.canvas_id == canvas_id,
            CanvasEdge.target_id == node_id,
            CanvasNode.kind == "image",
        ).execution_options(populate_existing=True)
    ))
    nodes = {source.id: source for _, source in pairs}
    incoming = sorted(
        (edge for edge, _ in pairs),
        key=lambda e: ({"reference": 0, "first_frame": 1, "last_frame": 2}[e.input], e.position, str(e.id)),
    )
    ids = {e.media_id for e in incoming if e.media_id is not None}
    rows = list(await db.scalars(select(ProjectMedia).where(
        ProjectMedia.project_id == project_id, ProjectMedia.id.in_(ids),
    ))) if ids else []
    outputs = {m.id: m for m in await media_outputs(db, user_id, rows)}
    return dict(
        revision=canvas.revision, content_revision=target.content_revision,
        items=[dict(
            reference_id=str(e.id), source_node_id=str(e.source_id),
            label=f"图片{index}", input=e.input, position=e.position,
            media_id=str(e.media_id) if e.media_id else None,
            available=e.media_id in outputs and outputs[e.media_id].source_verified,
            source_changed=e.media_id != nodes[e.source_id].media_id,
            media=outputs[e.media_id].model_dump(mode="json") if e.media_id in outputs else None,
        ) for index, e in enumerate(incoming, 1)],
    )


async def patch_canvas(db, project_id, user_id, canvas_id, payload: CanvasPatch):
    canvas = await _canvas(db, project_id, user_id, canvas_id, lock=True)
    _check_revision(canvas, payload.expected_revision)
    old_nodes, old_edges = await _graph(db, canvas_id)
    deleted_nodes = set(payload.delete_node_ids)
    nodes = {
        key: CanvasNodeInput.model_validate(n)
        for key, n in old_nodes.items()
        if key not in deleted_nodes
    }
    for node in nodes.values():
        if node.parent_id in deleted_nodes:
            node.parent_id = None
    for node in payload.upsert_nodes:
        if node.id in old_nodes and node.kind != old_nodes[node.id].kind:
            _invalid("已有节点不能更改类型，请创建新节点")
        nodes[node.id] = node
    for change in payload.update_nodes:
        if change.id not in old_nodes:
            raise AppException("节点不存在或不属于当前画布", code=40472, status_code=404)
        values = nodes[change.id].model_dump(mode="json")
        updates = change.model_dump(mode="json", exclude_unset=True, exclude={"id"})
        if "content" in updates:
            values["content"] = {**values["content"], **updates.pop("content")}
        values.update(updates)
        nodes[change.id] = CanvasNodeInput.model_validate(values)
    edges = {
        key: CanvasEdgeOut.model_validate(e)
        for key, e in old_edges.items()
        if key not in payload.delete_edge_ids
        and e.source_id not in deleted_nodes
        and e.target_id not in deleted_nodes
    }
    edges.update({e.id: CanvasEdgeOut.model_validate(e.model_dump()) for e in payload.upsert_edges})
    _validate_graph(nodes, edges)
    await _bind_media(db, project_id, nodes, edges, old_edges, payload.upsert_edges)
    await bind_text(db, project_id, user_id, canvas_id, nodes, old_nodes, edges, old_edges, payload.upsert_edges)
    previous_inputs, next_inputs = _inputs(old_edges), _inputs(edges)

    # Validate the entire proposed graph before mutating persistent rows.
    saved_nodes = {}
    for key, node in nodes.items():
        values = node.model_dump()
        values["content"] = node.content.model_dump(mode="json")
        row = old_nodes.get(key)
        if row is None:
            row = CanvasNode(canvas_id=canvas_id, **values, content_revision=1)
            db.add(row)
        else:
            if (
                CanvasContent.model_validate(row.content).model_dump(mode="json") != values["content"]
                or row.media_id != values["media_id"]
                or previous_inputs[key] != next_inputs[key]
            ):
                row.content_revision += 1
            if row.media_id != values["media_id"]:
                row.selected_generation_id = None
            for field, value in values.items():
                setattr(row, field, value)
        saved_nodes[key] = row
    await db.flush()
    for key, edge in old_edges.items():
        if key not in edges:
            await db.delete(edge)
    await db.flush()
    saved_edges = {}
    for key, edge in edges.items():
        values = edge.model_dump()
        values["text_snapshot"] = edge.text_snapshot.model_dump(mode="json") if edge.text_snapshot else None
        row = old_edges.get(key)
        if row is None:
            row = CanvasEdge(canvas_id=canvas_id, **values)
            db.add(row)
        else:
            for field, value in values.items():
                setattr(row, field, value)
        saved_edges[key] = row
    for key, node in old_nodes.items():
        if key not in nodes:
            await db.delete(node)
    if payload.name is not None:
        canvas.name = payload.name
    if payload.viewport is not None:
        canvas.viewport = payload.viewport.model_dump()
    canvas.revision += 1
    canvas.updated_at = beijing_datetime()
    await db.flush()
    result = _output(canvas, saved_nodes, saved_edges)
    await db.commit()
    return result


async def delete_canvas(db, project_id, user_id, canvas_id, expected_revision):
    canvas = await _canvas(db, project_id, user_id, canvas_id, lock=True)
    _check_revision(canvas, expected_revision)
    await db.delete(canvas)
    await db.commit()


async def patch_layout(db, project_id, user_id, canvas_id, payload: CanvasLayoutPatch):
    canvas = await _canvas(db, project_id, user_id, canvas_id, lock=True)
    _check_revision(canvas, payload.expected_revision)
    ids = {node.id for node in payload.nodes}
    nodes = {
        node.id: node for node in await db.scalars(
            select(CanvasNode).where(
                CanvasNode.canvas_id == canvas_id, CanvasNode.id.in_(ids),
            ).execution_options(populate_existing=True)
        )
    } if ids else {}
    if set(nodes) != ids:
        raise AppException("布局节点不存在或不属于当前画布", code=40472, status_code=404)
    for change in payload.nodes:
        for key, value in change.model_dump(exclude_unset=True, exclude={"id"}).items():
            setattr(nodes[change.id], key, value)
    if payload.viewport is not None:
        canvas.viewport = payload.viewport.model_dump()
    canvas.revision += 1
    canvas.updated_at = beijing_datetime()
    result = dict(
        id=str(canvas.id), revision=canvas.revision, viewport=canvas.viewport,
        nodes=[dict(id=str(change.id), **{
            key: getattr(nodes[change.id], key) for key in ("x", "y", "width", "height")
        }) for change in payload.nodes],
    )
    await db.commit()
    return result


async def reference_inputs(db, project_id, user_id, canvas_id, node_id):
    result = await image_inputs(db, project_id, user_id, canvas_id, node_id)
    for item in result["items"]:
        item["kind"] = "image"
    pairs = list(await db.execute(
        select(CanvasEdge, CanvasNode).join(
            CanvasNode,
            (CanvasNode.canvas_id == CanvasEdge.canvas_id) & (CanvasNode.id == CanvasEdge.source_id),
        ).where(
            CanvasEdge.canvas_id == canvas_id, CanvasEdge.target_id == node_id,
            CanvasEdge.input == "text", CanvasNode.kind == "text",
        ).order_by(CanvasEdge.position, CanvasEdge.id)
        .execution_options(populate_existing=True)
    ))
    results = await selected_results(db, project_id, user_id, canvas_id, [source for _, source in pairs])
    for index, (edge, source) in enumerate(pairs, 1):
        snapshot = CanvasEdgeOut.model_validate(edge).text_snapshot
        current = source_snapshot(source, edge.text_source, source.selected_generation_id, results)
        result["items"].append(dict(
            kind="text", reference_id=str(edge.id), source_node_id=str(source.id),
            label=f"文本{index}", input="text", position=edge.position, text_source=edge.text_source,
            available=snapshot is not None and bool(snapshot.text.strip()),
            source_changed=current != snapshot,
            text_snapshot=snapshot.model_dump(mode="json") if snapshot else None,
        ))
    return result
