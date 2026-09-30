"""Submit, inspect and select node generations; no provider calls during submission."""

from app.services.generation.task_events import task_phase

from uuid import UUID, uuid4

from sqlalchemy import func, select

from app.core.exceptions import AppException
from app.core.timezone import beijing_datetime
from app.models.ai_model import AiModel
from app.models.canvas_generation import CanvasGeneration
from app.models.task_record import UserTaskRecord
from app.schemas.project_canvas import CanvasContent
from app.services.billing.model_points import (
    calculate_submission_points_cost,
    ensure_model_minimum_balance,
)
from app.services.generation.submission import pending_generation
from app.services.generation.task_dispatch import dispatch_tasks_best_effort
from app.services.generation.provider_polling import provider_next_poll_seconds
from app.services.models.configuration import build_model_runtime_snapshot
from app.services.projects.canvases import _canvas, _graph
from app.services.projects.canvas_inputs import compile_inputs, invalid
from app.services.projects.queries import get_project_or_404


def check_content_revision(node, expected):
    if node.content_revision != expected:
        raise AppException(
            "节点内容已更新，请重新读取",
            code=40979,
            status_code=409,
            data={"current_content_revision": node.content_revision},
        )


def generation_out(generation, task):
    return dict(
        id=str(generation.id),
        canvas_id=str(generation.canvas_id),
        node_id=str(generation.node_id),
        task_record_id=str(task.id),
        status=task.status,
        content_revision=generation.content_revision,
        points_cost=task.points_cost,
        snapshot={
            key: value
            for key, value in generation.snapshot.items()
            if key != "seedance_provider_scope"
        },
        result=generation.result,
        failed_reason=(task.extra or {}).get("failed_reason"),
        created_at=generation.created_at.isoformat(),
    )


async def submit_generation(db, project_id, user_id, canvas_id, node_id, payload):
    # Canvas lock serializes submission with edits and duplicate requests.
    canvas = await _canvas(db, project_id, user_id, canvas_id, lock=True)
    existing = await db.scalar(
        select(CanvasGeneration).where(
            CanvasGeneration.project_id == project_id,
            CanvasGeneration.canvas_id == canvas_id,
            CanvasGeneration.node_id == node_id,
            CanvasGeneration.idempotency_key == payload.idempotency_key,
        )
    )
    if existing:
        if existing.content_revision != payload.expected_content_revision:
            raise AppException("幂等键已用于其他内容版本，请使用新键", code=40980, status_code=409)
        task = await db.get(UserTaskRecord, existing.task_record_id)
        result = generation_out(existing, task)
        result["revision"] = canvas.revision
        await db.commit()
        return result
    nodes, edges = await _graph(db, canvas_id)
    node = nodes.get(node_id)
    if node is None:
        raise AppException("节点不存在", code=40472, status_code=404)
    check_content_revision(node, payload.expected_content_revision)
    content = CanvasContent.model_validate(node.content)
    if node.kind == "group" or content.generation is None:
        invalid("请选择生成节点及对应模型")
    model = await db.scalar(
        select(AiModel).where(
            AiModel.id == content.generation.ai_model_id,
            AiModel.model_type == node.kind,
            AiModel.is_enabled.is_(True),
        )
    )
    if model is None:
        raise AppException("模型不存在、未启用或类型不匹配", code=40404, status_code=404)
    prompt, extra, inputs, scope = await compile_inputs(
        db, project_id, user_id, node, nodes, edges, model
    )
    await ensure_model_minimum_balance(db, user_id, model)
    cost = calculate_submission_points_cost(model, node.kind, extra)
    model_snapshot = vars(build_model_runtime_snapshot(model))
    model_snapshot["id"] = str(model.id)
    generation_id = uuid4()
    snapshot = dict(
        content=content.model_dump(mode="json"),
        model=model_snapshot,
        prompt=prompt,
        inputs=inputs,
        model_extra=extra,
        seedance_provider_scope=scope,
        kind=node.kind,
    )
    async with pending_generation(
        db,
        user_id=user_id,
        business_type="project",
        business_id=project_id,
        generation_type=node.kind,
        ai_model_id=model.id,
        title=f"画布生成：{node.title}"[:128],
        prompt=prompt,
        points_cost=cost,
        charge_remark="画布节点生成",
        extra={
            "canvas_generation_id": str(generation_id),
            "canvas_id": str(canvas_id),
            "node_id": str(node_id),
            "model_extra": extra,
            "canvas_snapshot": snapshot,
        },
        task_name="tasks.canvas_generation.run_canvas_generation",
    ) as task:
        generation = CanvasGeneration(
            id=generation_id,
            project_id=project_id,
            canvas_id=canvas_id,
            node_id=node_id,
            idempotency_key=payload.idempotency_key,
            content_revision=node.content_revision,
            task_record_id=task.id,
            snapshot=snapshot,
            result={},
        )
        db.add(generation)
        node.latest_generation_id = generation.id
        canvas.revision += 1
        canvas.updated_at = beijing_datetime()
        await db.flush()
        result = generation_out(generation, task)
        result["revision"] = canvas.revision
    await db.commit()
    await dispatch_tasks_best_effort(db, [task.id])
    return result


async def list_generations(db, project_id, user_id, canvas_id, node_id, page, page_size):
    # History remains readable after node/canvas deletion, through the owning project.
    await get_project_or_404(db, project_id, user_id)
    conditions = (
        CanvasGeneration.project_id == project_id,
        CanvasGeneration.canvas_id == canvas_id,
        CanvasGeneration.node_id == node_id,
    )
    total = await db.scalar(select(func.count()).select_from(CanvasGeneration).where(*conditions))
    rows = await db.execute(
        select(CanvasGeneration, UserTaskRecord)
        .join(UserTaskRecord, CanvasGeneration.task_record_id == UserTaskRecord.id)
        .where(*conditions)
        .order_by(CanvasGeneration.created_at.desc(), CanvasGeneration.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return dict(
        items=[generation_out(g, t) for g, t in rows], total=total, page=page, page_size=page_size
    )


async def batch_generations(db, project_id, user_id, canvas_id, ids):
    # Like node history, retained results remain readable after editor deletion.
    await get_project_or_404(db, project_id, user_id)
    ids = list(dict.fromkeys(ids))
    rows = await db.execute(
        select(
            CanvasGeneration.id, CanvasGeneration.canvas_id, CanvasGeneration.node_id,
            CanvasGeneration.task_record_id, CanvasGeneration.content_revision,
            CanvasGeneration.result, CanvasGeneration.created_at, UserTaskRecord.status,
            UserTaskRecord.generation_type, UserTaskRecord.points_cost,
            UserTaskRecord.extra["failed_reason"].label("failed_reason"),
        ).join(UserTaskRecord, CanvasGeneration.task_record_id == UserTaskRecord.id)
        .where(
            CanvasGeneration.id.in_(ids),
            CanvasGeneration.project_id == project_id,
            CanvasGeneration.canvas_id == canvas_id,
            UserTaskRecord.user_id == user_id,
            UserTaskRecord.business_type == "project",
            UserTaskRecord.business_id == project_id,
        )
    )
    by_id = {row.id: dict(row._mapping) for row in rows}
    items = [by_id[id_] for id_ in ids if id_ in by_id]
    return dict(
        items=items, total=len(items),
        missing_ids=[str(id_) for id_ in ids if id_ not in by_id],
    )


async def list_canvas_tasks(db, project_id, user_id, canvas_id, node_id, status, page, page_size):
    await _canvas(db, project_id, user_id, canvas_id)
    conditions = [
        CanvasGeneration.project_id == project_id,
        CanvasGeneration.canvas_id == canvas_id,
        UserTaskRecord.user_id == user_id,
        UserTaskRecord.business_type == "project",
        UserTaskRecord.business_id == project_id,
    ]
    if node_id is not None:
        conditions.append(CanvasGeneration.node_id == node_id)
    if status == "active":
        conditions.append(UserTaskRecord.status.in_(["pending", "running"]))
    elif status != "all":
        conditions.append(UserTaskRecord.status == status)
    total = await db.scalar(
        select(func.count()).select_from(CanvasGeneration)
        .join(UserTaskRecord, CanvasGeneration.task_record_id == UserTaskRecord.id)
        .where(*conditions)
    )
    # Polling summaries do not load prompts, frozen model snapshots or result bodies.
    rows = await db.execute(
        select(
            CanvasGeneration.id.label("generation_id"), CanvasGeneration.node_id,
            CanvasGeneration.task_record_id, CanvasGeneration.content_revision,
            CanvasGeneration.created_at, UserTaskRecord.status, UserTaskRecord.generation_type,
            UserTaskRecord.points_cost, UserTaskRecord.extra["next_poll_seconds"].label("poll_hint"),
            UserTaskRecord.extra["generation_phase"].label("phase"),
        ).join(UserTaskRecord, CanvasGeneration.task_record_id == UserTaskRecord.id)
        .where(*conditions)
        .order_by(CanvasGeneration.created_at.desc(), CanvasGeneration.id.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )
    items = []
    for row in rows:
        items.append(dict(
            generation_id=str(row.generation_id), task_record_id=str(row.task_record_id),
            node_id=str(row.node_id), generation_type=row.generation_type, status=row.status,
            phase=task_phase(row.status, {"generation_phase": row.phase}),
            content_revision=row.content_revision, points_cost=row.points_cost,
            created_at=row.created_at,
            stop_polling=row.status not in {"pending", "running"},
            next_poll_seconds=provider_next_poll_seconds(
                row.generation_type, row.status, {"next_poll_seconds": row.poll_hint},
            ),
        ))
    return dict(items=items, total=total, page=page, page_size=page_size)


async def select_generation(db, project_id, user_id, canvas_id, node_id, generation_id, payload):
    canvas = await _canvas(db, project_id, user_id, canvas_id, lock=True)
    nodes, _ = await _graph(db, canvas_id)
    node = nodes.get(node_id)
    if node is None:
        raise AppException("节点不存在", code=40472, status_code=404)
    check_content_revision(node, payload.expected_content_revision)
    generation = await db.scalar(
        select(CanvasGeneration).where(
            CanvasGeneration.id == generation_id,
            CanvasGeneration.project_id == project_id,
            CanvasGeneration.canvas_id == canvas_id,
            CanvasGeneration.node_id == node_id,
        )
    )
    if generation is None:
        raise AppException("生成历史不存在", code=40473, status_code=404)
    task = await db.get(UserTaskRecord, generation.task_record_id)
    if (
        task.status != "success"
        or not generation.result
        or node.kind != generation.snapshot["kind"]
    ):
        invalid("只能选择对应类型的成功生成结果")
    ids = generation.result.get("media_ids") or []
    if payload.result_index >= (len(ids) if ids else 1):
        invalid("结果序号不存在")
    node.media_id = UUID(ids[payload.result_index]) if ids else None
    node.selected_generation_id = generation.id
    node.latest_generation_id = None  # Explicit selection fences all in-flight automatic updates.
    node.content_revision += 1
    canvas.revision += 1
    canvas.updated_at = beijing_datetime()
    await db.commit()
    return dict(
        selected_generation_id=str(generation.id),
        media_id=str(node.media_id) if node.media_id else None,
        content_revision=node.content_revision,
        revision=canvas.revision,
    )
