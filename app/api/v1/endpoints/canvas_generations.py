from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.v1.endpoints.project_dependencies import require_standard_project
from app.core.responses import success
from app.db.session import get_db
from app.models.user import User
from app.schemas.canvas_generation import CanvasGenerationSelect, CanvasGenerationSubmit
from app.services.projects import canvas_generations

router = APIRouter(
    prefix="/projects/{project_id}/canvases/{canvas_id}/nodes/{node_id}/generations",
    dependencies=[Depends(require_standard_project)],
)


@router.post("")
async def submit(
    project_id: UUID,
    canvas_id: UUID,
    node_id: UUID,
    payload: CanvasGenerationSubmit,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return success(
        data=await canvas_generations.submit_generation(
            db, project_id, user.id, canvas_id, node_id, payload
        )
    )


@router.get("")
async def history(
    project_id: UUID,
    canvas_id: UUID,
    node_id: UUID,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return success(
        data=await canvas_generations.list_generations(
            db, project_id, user.id, canvas_id, node_id, page, page_size
        )
    )


@router.post("/{generation_id}/select")
async def select_result(
    project_id: UUID,
    canvas_id: UUID,
    node_id: UUID,
    generation_id: UUID,
    payload: CanvasGenerationSelect,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return success(
        data=await canvas_generations.select_generation(
            db, project_id, user.id, canvas_id, node_id, generation_id, payload
        )
    )
