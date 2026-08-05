from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.exceptions import AppException
from app.core.responses import success
from app.db.session import get_db
from app.models.user import User
from app.schemas.task_record import (
    UserTaskRecordBatchRequest,
    TaskRecordOptionsOut,
    UserTaskRecordListOut,
    UserTaskRecordOut,
)
from app.services.task_records import (
    get_task_record_options,
    get_task_record_or_404,
    list_user_task_records_by_ids,
    list_user_task_records,
)
from app.services.provider_polling import provider_next_poll_seconds

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
    dumped = data.model_dump(mode="json")
    dumped["items"] = [_clean_terminal_retry_extra(item) for item in dumped["items"]]
    return success(data=dumped)


@router.get("/options")
async def my_task_record_options(
    current_user: User = Depends(get_current_user),
):
    data = TaskRecordOptionsOut.model_validate(get_task_record_options())
    return success(data=data.model_dump(mode="json"))


@router.post("/batch")
async def my_task_record_batch(
    payload: UserTaskRecordBatchRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    records = await list_user_task_records_by_ids(db, current_user.id, payload.ids)
    items = []
    next_poll_candidates = []
    for record in records:
        item = _dump_task_record(record)
        if item["next_poll_seconds"]:
            next_poll_candidates.append(item["next_poll_seconds"])
        items.append(item)

    aggregate_next_poll_seconds = min(next_poll_candidates) if next_poll_candidates else None
    response.headers["Cache-Control"] = "no-store"
    if aggregate_next_poll_seconds:
        response.headers["X-Next-Poll-Seconds"] = str(aggregate_next_poll_seconds)
    return success(
        data={
            "items": items,
            "total": len(items),
            "stop_polling": all(item["stop_polling"] for item in items) if items else True,
            "next_poll_seconds": aggregate_next_poll_seconds,
        }
    )


@router.get("/{task_record_id}")
async def my_task_record_detail(
    task_record_id: str,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    parsed_task_record_id = _parse_task_record_id(task_record_id)
    record = await get_task_record_or_404(db, parsed_task_record_id, user_id=current_user.id)
    data = _dump_task_record(record)
    response.headers["Cache-Control"] = "no-store"
    if data["next_poll_seconds"]:
        response.headers["X-Next-Poll-Seconds"] = str(data["next_poll_seconds"])
    return success(data=data)


def _dump_task_record(record) -> dict:
    data = UserTaskRecordOut.model_validate(record).model_dump(mode="json")
    data = _clean_terminal_retry_extra(data)
    data["stop_polling"] = record.status in {"success", "failed"}
    data["next_poll_seconds"] = _task_record_next_poll_seconds(
        record.generation_type,
        record.status,
        data.get("extra") or {},
    )
    return data


def _task_record_next_poll_seconds(generation_type: str, status: str, extra: dict) -> Optional[int]:
    return provider_next_poll_seconds(generation_type, status, extra)


def _clean_terminal_retry_extra(data: dict) -> dict:
    if data.get("status") in {"pending", "running"}:
        return data
    extra = data.get("extra")
    if isinstance(extra, dict):
        extra.pop("retry_reason", None)
        extra.pop("next_poll_seconds", None)
    return data


def _parse_task_record_id(value: str) -> UUID:
    if value in {"", "None", "none", "null", "undefined"}:
        raise AppException(
            "任务ID不能为空，请确认提交任务接口返回了 task_record_id", code=40018, status_code=400
        )
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise AppException("任务ID格式不正确", code=40018, status_code=400) from exc
