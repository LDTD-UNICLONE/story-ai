from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_current_admin_user
from app.core.responses import success
from app.models.user import User
from app.services.system_logs import read_system_log_tail

router = APIRouter(prefix="/admin/system-logs")


@router.get("")
async def admin_system_logs(
    lines: int = Query(default=300, ge=1, le=2000),
    keyword: Optional[str] = Query(default=None, max_length=128),
    current_admin: User = Depends(get_current_admin_user),
):
    return success(data=read_system_log_tail(lines=lines, keyword=keyword or ""))
