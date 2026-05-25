from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.responses import success
from app.db.session import get_db
from app.schemas.style import StyleBaseOut, StyleOut
from app.services.styles import get_enabled_style_or_404, list_enabled_styles

router = APIRouter(prefix="/styles")


@router.get("")
async def style_options(db: AsyncSession = Depends(get_db)):
    styles = await list_enabled_styles(db)
    data = [StyleBaseOut.model_validate(item).model_dump(mode="json") for item in styles]
    return success(data=data)


@router.get("/{style_id}")
async def style_detail(style_id: UUID, db: AsyncSession = Depends(get_db)):
    style = await get_enabled_style_or_404(db, style_id)
    return success(data=StyleOut.model_validate(style).model_dump(mode="json"))
