from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin_user
from app.core.responses import success
from app.db.session import get_db
from app.models.user import User
from app.schemas.task_record import (
    AdminTaskRecordListOut,
    AdminTaskRecordInterruptRequest,
    TaskRecordOptionsOut,
    AdminTaskRecordListItemOut,
    UserTaskRecordOut,
)
from app.services.generation.task_records import (
    get_task_record_options,
    get_task_record_or_404,
    interrupt_task_record,
    list_admin_task_record_summaries,
)

router = APIRouter(prefix="/admin/task-records")


def _dump_admin_task_record(record) -> dict:
    data = UserTaskRecordOut.model_validate(record).model_dump(mode="json")
    provider_cost_billing = (record.extra or {}).get("provider_cost_billing")
    if isinstance(provider_cost_billing, dict):
        data["provider_cost_billing"] = provider_cost_billing
    return data


@router.get("")
async def admin_task_records(
    user_id: Optional[UUID] = Query(default=None),
    business_type: Optional[str] = Query(default=None, max_length=32),
    generation_type: Optional[str] = Query(default=None, max_length=32),
    status: Optional[str] = Query(default=None, max_length=32),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    records, total = await list_admin_task_record_summaries(
        db,
        user_id=user_id,
        business_type=business_type,
        generation_type=generation_type,
        status=status,
        page=page,
        page_size=page_size,
    )
    data = AdminTaskRecordListOut(
        items=[AdminTaskRecordListItemOut.model_validate(item) for item in records],
        total=total,
        page=page,
        page_size=page_size,
    )
    return success(data=data.model_dump(mode="json"))


@router.get("/options")
async def admin_task_record_options(
    current_admin: User = Depends(get_current_admin_user),
):
    data = TaskRecordOptionsOut.model_validate(get_task_record_options())
    return success(data=data.model_dump(mode="json"))


@router.get("/{task_record_id}")
async def admin_task_record_detail(
    task_record_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    record = await get_task_record_or_404(db, task_record_id)
    return success(data=_dump_admin_task_record(record))


@router.post("/{task_record_id}/interrupt")
async def admin_interrupt_task_record(
    task_record_id: UUID,
    payload: Optional[AdminTaskRecordInterruptRequest] = None,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    record = await interrupt_task_record(
        db,
        task_record_id=task_record_id,
        admin_user_id=current_admin.id,
        reason=payload.reason if payload else None,
    )
    return success(data=_dump_admin_task_record(record))
