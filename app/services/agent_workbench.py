from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.public_messages import sanitize_public_data, sanitize_public_message
from app.models.agent_production import AgentControllerState
from app.services.agent_production_monitoring import (
    get_agent_production_costs,
    get_agent_production_matrix,
)
from app.services.agent_productions import get_agent_production_or_404
from app.services.agent_workflow import uses_agent_workflow_v2


REVIEW_ACTIONS = {
    "script_review": "review_script",
    "episode_plan_review": "review_episode_plan",
    "story_bible_review": "review_story_bible",
    "core_asset_change_review": "review_core_asset_changes",
    "pilot_storyboard_review": "review_pilot_storyboards",
    "pilot_images_ready": "generate_pilot_videos",
    "pilot_review": "review_pilot",
}


async def get_agent_production_workbench(
    db: AsyncSession,
    production_id: UUID,
    user_id: UUID,
) -> Dict[str, Any]:
    production = await get_agent_production_or_404(db, production_id, user_id)
    matrix = await get_agent_production_matrix(db, production_id, user_id)
    costs = await get_agent_production_costs(db, production_id, user_id)
    state_result = await db.execute(
        select(AgentControllerState).where(AgentControllerState.production_id == production_id)
    )
    controller = state_result.scalar_one_or_none()
    next_action, available_actions = resolve_workbench_actions(
        production.status,
        production.current_stage,
        configuration_confirmed=(
            not uses_agent_workflow_v2(production)
            or bool((production.extra or {}).get("configuration_confirmed"))
        ),
    )
    controller_data = None
    if controller is not None:
        controller_data = {
            "status": controller.status,
            "lease_expires_at": controller.lease_expires_at,
            "attempt_count": controller.attempt_count,
            "last_started_at": controller.last_started_at,
            "last_finished_at": controller.last_finished_at,
            "last_error": sanitize_public_message(controller.last_error),
            "last_result": sanitize_public_data(controller.last_result or {}),
        }
    return {
        "production": production,
        "matrix": matrix,
        "costs": costs,
        "controller": controller_data,
        "next_action": next_action,
        "available_actions": available_actions,
    }


def resolve_workbench_actions(
    status: str,
    current_stage: str,
    configuration_confirmed: bool = True,
) -> Tuple[Optional[str], List[str]]:
    if status == "cancelled":
        return None, []
    if status == "completed":
        return "review_results", ["review_results"]
    if status == "draft" and not configuration_confirmed:
        return "configure", ["configure", "cancel"]
    if status == "draft":
        return "start", ["start", "cancel"]
    if status == "paused":
        return "resume", ["resume", "cancel"]
    if status == "partially_failed":
        return "review_exceptions", ["review_exceptions", "resume", "cancel"]

    review_action = REVIEW_ACTIONS.get(current_stage)
    if review_action is not None:
        return review_action, [review_action, "cancel"]
    if current_stage == "batch_production":
        return "generate_storyboards", ["generate_storyboards", "pause", "cancel"]
    if current_stage == "batch_storyboards":
        return "monitor_storyboards", ["monitor_storyboards", "pause", "cancel"]
    if current_stage == "episode_videos":
        return "generate_episode_videos", [
            "generate_episode_videos",
            "pause",
            "cancel",
        ]
    if current_stage.startswith("batch_"):
        return "monitor_batch", ["monitor_batch", "pause", "cancel"]
    if current_stage.startswith("pilot_") or current_stage == "pilot_production":
        return "monitor_pilot", ["monitor_pilot", "pause", "cancel"]
    if current_stage == "core_assets":
        return "manage_core_assets", ["manage_core_assets", "pause", "cancel"]
    if status == "waiting_approval":
        return "review_checkpoint", ["review_checkpoint", "cancel"]
    return "monitor_progress", ["monitor_progress", "pause", "cancel"]
