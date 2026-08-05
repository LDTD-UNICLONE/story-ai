from typing import Optional

from fastapi import APIRouter, Depends, Query
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_current_admin_user
from app.core.responses import success
from app.models.user import User
from app.services.system_logs import read_system_log_tail

router = APIRouter(prefix="/admin/system-logs")


@router.get("")
async def admin_system_logs(
    lines: int = Query(default=300, ge=1, le=2000),
    keyword: Optional[str] = Query(default=None, max_length=128),
    log_file: str = Query(default="app", max_length=64),
    level: Optional[str] = Query(default=None, max_length=16),
    request_id: Optional[str] = Query(default=None, max_length=128),
    user_id: Optional[str] = Query(default=None, max_length=64),
    current_admin: User = Depends(get_current_admin_user),
):
    data = await run_in_threadpool(
        read_system_log_tail,
        lines=lines,
        keyword=keyword or "",
        log_file=log_file,
        level=level,
        request_id=request_id,
        user_id=user_id,
    )
    return success(data=data)
