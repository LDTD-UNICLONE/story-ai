from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.v1.endpoints.project_dependencies import require_standard_project
from app.core.exceptions import AppException
from app.core.responses import success
from app.db.session import get_db
from app.models.canvas_import_record import CanvasImportRecord
from app.models.user import User
from app.services.projects.queries import get_project_or_404

router = APIRouter(
    prefix="/projects/{project_id}/import-records",
    dependencies=[Depends(require_standard_project)],
)


def _output(row, *, detail=False):
    data = dict(
        id=str(row.id), project_id=str(row.project_id), source_type=row.source_type,
        source_id=str(row.source_id), nodes=row.nodes, warnings=row.warnings,
        created_at=row.created_at,
    )
    if detail:
        data["data"] = row.data
    return data


@router.get("")
async def list_records(
    project_id: UUID, page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
):
    await get_project_or_404(db, project_id, user.id)
    condition = CanvasImportRecord.project_id == project_id
    total = await db.scalar(select(func.count()).select_from(CanvasImportRecord).where(condition))
    rows = await db.scalars(select(CanvasImportRecord).where(condition)
                            .order_by(CanvasImportRecord.created_at, CanvasImportRecord.id)
                            .offset((page - 1) * page_size).limit(page_size))
    return success(data=dict(items=[_output(row) for row in rows], total=total,
                             page=page, page_size=page_size))


@router.get("/{record_id}")
async def get_record(
    project_id: UUID, record_id: UUID,
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
):
    await get_project_or_404(db, project_id, user.id)
    row = await db.scalar(select(CanvasImportRecord).where(
        CanvasImportRecord.id == record_id, CanvasImportRecord.project_id == project_id,
    ))
    if row is None:
        raise AppException("迁移记录不存在", code=40474, status_code=404)
    return success(data=_output(row, detail=True))
