from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.responses import success
from app.db.session import get_db
from app.models.user import User
from app.schemas.task_record import (
    TaskRecordOptionsOut,
    UserTaskRecordListOut,
    UserTaskRecordOut,
)
from app.services.task_records import (
    get_task_record_options,
    get_task_record_or_404,
    list_user_task_records,
)

router = APIRouter(prefix="/task-records")


@router.get("")
async def my_task_records(
    business_type: Optional[str] = Query(default=None, max_length=32),
    generation_type: Optional[str] = Query(default=None, max_length=32),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    records, total = await list_user_task_records(
        db,
        user_id=current_user.id,
        business_type=business_type,
        generation_type=generation_type,
        page=page,
        page_size=page_size,
    )
    data = UserTaskRecordListOut(
        items=[UserTaskRecordOut.model_validate(item) for item in records],
        total=total,
        page=page,
        page_size=page_size,
    )
    return success(data=data.model_dump(mode="json"))


@router.get("/options")
async def my_task_record_options(
    current_user: User = Depends(get_current_user),
):
    data = TaskRecordOptionsOut.model_validate(get_task_record_options())
    return success(data=data.model_dump(mode="json"))


@router.get("/{task_record_id}")
async def my_task_record_detail(
    task_record_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    record = await get_task_record_or_404(db, task_record_id, user_id=current_user.id)
    return success(data=UserTaskRecordOut.model_validate(record).model_dump(mode="json"))
