from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.v1.endpoints.project_dependencies import require_standard_project
from app.core.responses import success
from app.db.session import get_db
from app.models.user import User
from app.schemas.project_media import ProjectMediaBatchRequest, ProjectMediaImport
from app.services.projects import media

router = APIRouter(
    prefix="/projects/{project_id}/media", dependencies=[Depends(require_standard_project)]
)


@router.post("/images")
async def upload_image(
    project_id: UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await media.upload_image(db, project_id, user.id, file)
    return success(data=result.model_dump(mode="json"))


@router.post("/imports")
async def import_image(
    project_id: UUID,
    payload: ProjectMediaImport,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await media.import_image(db, project_id, user.id, payload)
    return success(data=result.model_dump(mode="json"))


@router.get("")
async def list_media(
    project_id: UUID,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return success(data=await media.list_media(db, project_id, user.id, page, page_size))


@router.get("/{media_id}")
async def get_media(
    project_id: UUID,
    media_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = await media.get_media(db, project_id, user.id, media_id)
    result = (await media.media_outputs(db, user.id, [row]))[0]
    return success(data=result.model_dump(mode="json"))


@router.post("/batch")
async def batch_media(
    project_id: UUID,
    payload: ProjectMediaBatchRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return success(data=await media.batch_media(db, project_id, user.id, payload.ids))


@router.post("/{media_id}/review")
async def review_media(
    project_id: UUID,
    media_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await media.review_media(db, project_id, user.id, media_id)
    return success(data=result.model_dump(mode="json"))
