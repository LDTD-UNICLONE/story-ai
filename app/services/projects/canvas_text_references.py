"""Resolve owned text sources and pin them without running upstream nodes."""

from sqlalchemy import select

from app.core.exceptions import AppException
from app.models.canvas_generation import CanvasGeneration
from app.models.task_record import UserTaskRecord
from app.schemas.project_canvas import CanvasContent, CanvasTextSnapshot


async def selected_results(db, project_id, user_id, canvas_id, nodes):
    ids = {n.selected_generation_id for n in nodes if n.selected_generation_id is not None}
    if not ids:
        return {}
    rows = await db.execute(select(
        CanvasGeneration.id, CanvasGeneration.node_id, CanvasGeneration.result["text"].label("text"),
    ).join(
        UserTaskRecord, CanvasGeneration.task_record_id == UserTaskRecord.id,
    ).where(
        CanvasGeneration.id.in_(ids), CanvasGeneration.project_id == project_id,
        CanvasGeneration.canvas_id == canvas_id, UserTaskRecord.user_id == user_id,
        UserTaskRecord.business_type == "project", UserTaskRecord.business_id == project_id,
        UserTaskRecord.generation_type == "text", UserTaskRecord.status == "success",
    ))
    return {row.id: row for row in rows}


def source_snapshot(node, text_source, selected_id, results):
    if text_source == "input":
        text, generation_id = CanvasContent.model_validate(node.content).text, None
    else:
        result = results.get(selected_id)
        if result is None or result.node_id != node.id:
            return None
        text, generation_id = result.text, result.id
        if not isinstance(text, str):
            return None
    if len(text) > 10000:
        return None
    return CanvasTextSnapshot(text=text, generation_id=generation_id)


async def bind_text(db, project_id, user_id, canvas_id, nodes, old_nodes, edges, old_edges, changes):
    text_changes = [c for c in changes if c.input == "text"]
    results = await selected_results(
        db, project_id, user_id, canvas_id,
        [old_nodes[c.source_id] for c in text_changes if c.source_id in old_nodes and c.text_source == "result"],
    ) if text_changes else {}
    for change in changes:
        edge = edges[change.id]
        previous = old_edges.get(change.id)
        if edge.input != "text":
            edge.text_snapshot = None
            continue
        if (previous is not None and previous.input == "text"
                and previous.source_id == edge.source_id and previous.text_source == edge.text_source
                and not change.refresh_text):
            edge.text_snapshot = CanvasTextSnapshot.model_validate(previous.text_snapshot)
            continue
        source = nodes[edge.source_id]
        selected_id = getattr(old_nodes.get(edge.source_id), "selected_generation_id", None)
        snapshot = source_snapshot(source, edge.text_source, selected_id, results)
        if snapshot is None:
            raise AppException("文本来源不可用、尚无选中成功结果或超过10000字符", code=40070)
        edge.text_snapshot = snapshot
