"""Save canvas results, conditionally select them, and recover points settlement."""

import logging
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy import select

from app.core.exceptions import AppException
from app.core.timezone import beijing_datetime
from app.models.canvas_generation import CanvasGeneration
from app.models.project import Project
from app.models.project_canvas import CanvasNode, ProjectCanvas
from app.models.project_media import ProjectMedia
from app.models.task_record import UserTaskRecord
from app.services.billing.model_points import (
    settle_image_task_points,
    settle_text_task_points,
    settle_video_task_points,
)
from app.services.generation.media import extract_result_urls

logger = logging.getLogger(__name__)


def runtime_model(record):
    return SimpleNamespace(**record.extra["canvas_snapshot"]["model"])


async def save_result(db, record, model_result):
    generation = await db.scalar(
        select(CanvasGeneration).where(CanvasGeneration.task_record_id == record.id)
    )
    if generation is None or generation.result:
        return
    kind = generation.snapshot["kind"]
    urls = extract_result_urls(model_result.content) if kind != "text" else []
    if (kind != "text" and not urls) or not model_result.content.strip():
        raise AppException("模型未返回有效结果", code=50231, status_code=502)
    media_ids = []
    for index, url in enumerate(urls):
        row = ProjectMedia(
            project_id=generation.project_id,
            source_key=f"canvas:{generation.id}:{index}",
            source_type="canvas_generation",
            source_id=generation.id,
            media_type=kind,
            upload={"url": url, "filename": f"{generation.id}-{index}"},
        )
        db.add(row)
        await db.flush()
        media_ids.append(str(row.id))
    generation.result = dict(
        text=model_result.content if kind == "text" else None,
        urls=urls,
        media_ids=media_ids,
        auto_applied=False,
    )
    canvas = await db.scalar(
        select(ProjectCanvas)
        .join(Project, Project.id == ProjectCanvas.project_id)
        .where(
            ProjectCanvas.id == generation.canvas_id,
            ProjectCanvas.project_id == generation.project_id,
            Project.user_id == record.user_id,
            Project.is_enabled.is_(True),
            Project.project_kind == "standard",
        )
        .with_for_update(of=ProjectCanvas)
        .execution_options(populate_existing=True)
    )
    if canvas is None:
        return
    node = await db.scalar(
        select(CanvasNode)
        .where(
            CanvasNode.canvas_id == canvas.id,
            CanvasNode.id == generation.node_id,
        )
        .execution_options(populate_existing=True)
    )
    if (
        node is None
        or node.latest_generation_id != generation.id
        or node.content_revision != generation.content_revision
        or node.kind != kind
    ):
        return
    node.selected_generation_id = generation.id
    node.media_id = UUID(media_ids[0]) if media_ids else None
    node.content_revision += 1
    canvas.revision += 1
    canvas.updated_at = beijing_datetime()
    generation.result = {**generation.result, "auto_applied": True}


async def settle_completed(db, task_id):
    """Billing failure never discards successful media; beat retries settlement."""
    task = await db.scalar(
        select(UserTaskRecord)
        .where(UserTaskRecord.id == task_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if task is None or task.status != "success" or task.extra.get("points_settled"):
        await db.rollback()
        return False
    try:
        model = runtime_model(task)
        settle = {
            "text": settle_text_task_points,
            "image": settle_image_task_points,
            "video": settle_video_task_points,
        }[task.generation_type]
        data = (
            task.extra["model_extra"]
            if task.generation_type == "video"
            else task.extra["model_result_extra"]
        )
        await settle(db, task, model, data, remark_prefix="画布节点生成")
        await db.commit()
        return True
    except Exception:
        await db.rollback()
        logger.exception("Canvas points settlement deferred: task_record_id=%s", task_id)
        return False
