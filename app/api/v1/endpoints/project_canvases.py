from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.v1.endpoints.project_dependencies import require_standard_project
from app.core.responses import success
from app.db.session import get_db
from app.models.user import User
from app.schemas.project_canvas import CanvasCreate, CanvasLayoutPatch, CanvasPatch
from app.schemas.canvas_generation import CanvasGenerationBatchRequest, CanvasTaskStatus
from app.services.projects import canvas_generations, canvases

router = APIRouter(
    prefix="/projects/{project_id}/canvases", dependencies=[Depends(require_standard_project)]
)


@router.post("")
async def create_canvas(
    project_id: UUID,
    payload: CanvasCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await canvases.create_canvas(db, project_id, user.id, payload)
    return success(data=result.model_dump(mode="json"))


@router.get("")
async def list_canvases(
    project_id: UUID,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return success(data=await canvases.list_canvases(db, project_id, user.id, page, page_size))


@router.get("/{canvas_id}")
async def get_canvas(
    project_id: UUID,
    canvas_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await canvases.get_canvas(db, project_id, user.id, canvas_id)
    return success(data=result.model_dump(mode="json"))


@router.patch("/{canvas_id}")
async def patch_canvas(
    project_id: UUID,
    canvas_id: UUID,
    payload: CanvasPatch,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await canvases.patch_canvas(db, project_id, user.id, canvas_id, payload)
    return success(data=result.model_dump(mode="json"))


@router.get("/{canvas_id}/nodes/{node_id}/image-inputs")
async def image_inputs(
    project_id: UUID,
    canvas_id: UUID,
    node_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return success(data=await canvases.image_inputs(db, project_id, user.id, canvas_id, node_id))


@router.get("/{canvas_id}/nodes/{node_id}/reference-inputs")
async def reference_inputs(
    project_id: UUID,
    canvas_id: UUID,
    node_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return success(data=await canvases.reference_inputs(db, project_id, user.id, canvas_id, node_id))


@router.patch("/{canvas_id}/layout")
async def patch_layout(
    project_id: UUID,
    canvas_id: UUID,
    payload: CanvasLayoutPatch,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return success(data=await canvases.patch_layout(db, project_id, user.id, canvas_id, payload))


@router.get("/{canvas_id}/tasks")
async def list_tasks(
    project_id: UUID,
    canvas_id: UUID,
    response: Response,
    node_id: UUID | None = None,
    status: CanvasTaskStatus = "active",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    response.headers["Cache-Control"] = "no-store"
    return success(data=await canvas_generations.list_canvas_tasks(
        db, project_id, user.id, canvas_id, node_id, status, page, page_size,
    ))


@router.post("/{canvas_id}/generations/batch")
async def batch_generations(
    project_id: UUID,
    canvas_id: UUID,
    payload: CanvasGenerationBatchRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    response.headers["Cache-Control"] = "no-store"
    return success(data=await canvas_generations.batch_generations(
        db, project_id, user.id, canvas_id, payload.ids,
    ))


@router.delete("/{canvas_id}")
async def delete_canvas(
    project_id: UUID,
    canvas_id: UUID,
    expected_revision: int = Query(ge=1),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await canvases.delete_canvas(db, project_id, user.id, canvas_id, expected_revision)
    return success(data={"id": str(canvas_id), "deleted": True})
