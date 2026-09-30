from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.responses import success
from app.db.session import get_db
from app.schemas.ai_model import AiModelOptionOut, AiModelOut
from app.services.models.catalog import (
    get_ai_model_or_404,
    list_ai_model_options,
    resolve_ai_model_configuration,
)


def dump_ai_model(model, schema):
    data = schema.model_validate(model).model_dump(mode="json")
    data["configuration"] = resolve_ai_model_configuration(model)
    return data


router = APIRouter(prefix="/models")


@router.get("/options")
async def model_options(
    vendor: Optional[str] = Query(default=None, max_length=64),
    model_type: Optional[str] = Query(default=None, max_length=64),
    db: AsyncSession = Depends(get_db),
):
    models = await list_ai_model_options(db, vendor=vendor, model_type=model_type)
    data = [dump_ai_model(item, AiModelOptionOut) for item in models]
    return success(data=data)


@router.get("/{ai_model_id}")
async def model_detail(
    ai_model_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    ai_model = await get_ai_model_or_404(db, ai_model_id, only_enabled=True)
    return success(data=dump_ai_model(ai_model, AiModelOut))
