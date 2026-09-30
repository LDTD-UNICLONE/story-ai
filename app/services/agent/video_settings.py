from typing import Any
from uuid import UUID

from app.core.exceptions import AppException
from app.integrations.apimart import APIMART_VENDOR
from app.integrations.apimart_video_specs import merge_video_capabilities
from app.models.agent_production import AgentProduction
from app.services.models.configuration import model_request_capabilities


def require_agent_video_defaults(
    production: AgentProduction,
    video_model_id: UUID,
    video_resolution: str,
) -> None:
    if production.mode != "automatic":
        return

    spec = production.production_spec or {}
    expected_model_id = _optional_uuid(spec.get("video_model_id"))
    expected_resolution = str(spec.get("video_resolution") or "")
    if expected_model_id == video_model_id and expected_resolution == video_resolution:
        return

    raise AppException(
        "自动模式视频配置必须与项目创建时的选择一致",
        code=40987,
        status_code=409,
        data={
            "expected_video_model_id": (
                str(expected_model_id) if expected_model_id is not None else None
            ),
            "expected_video_resolution": expected_resolution or None,
        },
    )


def provider_video_duration(model: Any, duration_seconds: int) -> int | None:
    if getattr(model, "vendor", None) != APIMART_VENDOR:
        return duration_seconds
    capabilities = merge_video_capabilities(
        str(model.model_id),
        model_request_capabilities(model),
    )
    if not (capabilities.get("duration") or {}).get("controllable", True):
        return None
    return duration_seconds


def _optional_uuid(value: Any) -> UUID | None:
    try:
        return UUID(str(value)) if value else None
    except (TypeError, ValueError):
        return None
