from typing import Dict, Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException
from app.models.ai_model import AiModel


AGENT_MODEL_TYPES = ("text", "image", "video")
FIXED_AGENT_MODEL_IDS = {
    "text": "gpt-5.5",
    "image": "gpt-image-2",
}


async def resolve_fixed_agent_models(
    db: AsyncSession,
    required_types: Iterable[str] = FIXED_AGENT_MODEL_IDS,
) -> Dict[str, AiModel]:
    model_types = tuple(dict.fromkeys(required_types))
    expected_ids = {
        model_type: FIXED_AGENT_MODEL_IDS[model_type]
        for model_type in model_types
    }
    result = await db.execute(
        select(AiModel).where(
            AiModel.model_id.in_(expected_ids.values()),
            AiModel.is_enabled.is_(True),
        )
    )
    models = {
        model_type: model
        for model in result.scalars().all()
        for model_type, model_id in expected_ids.items()
        if model.model_id == model_id and model.model_type == model_type
    }
    missing = [
        f"{model_type}:{expected_ids[model_type]}"
        for model_type in model_types
        if model_type not in models
    ]
    if missing:
        raise AppException(
            "Agent 固定模型尚未配置",
            code=40980,
            status_code=409,
            data={"missing_models": missing},
        )
    return models


async def get_agent_video_model(
    db: AsyncSession,
    video_model_id: UUID,
) -> AiModel:
    result = await db.execute(
        select(AiModel).where(
            AiModel.id == video_model_id,
            AiModel.model_type == "video",
            AiModel.is_enabled.is_(True),
        )
    )
    model = result.scalar_one_or_none()
    if model is None:
        raise AppException(
            "视频模型不存在、未启用或类型不匹配",
            code=40404,
            status_code=404,
        )
    return model


async def resolve_agent_default_models(
    db: AsyncSession,
    required_types: Iterable[str] = AGENT_MODEL_TYPES,
) -> Dict[str, AiModel]:
    model_types = tuple(dict.fromkeys(required_types))
    result = await db.execute(
        select(AiModel).where(
            AiModel.model_type.in_(model_types),
            AiModel.is_agent_default.is_(True),
            AiModel.is_enabled.is_(True),
        )
    )
    models = {model.model_type: model for model in result.scalars().all()}
    missing = [model_type for model_type in model_types if model_type not in models]
    if missing:
        raise AppException(
            "系统默认模型尚未配置",
            code=40980,
            status_code=409,
            data={"missing_model_types": missing},
        )
    return models
