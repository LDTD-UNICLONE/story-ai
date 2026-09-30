from typing import Optional

from app.models.agent_production import AgentCheckpoint, AgentProduction, AgentStep


def agent_pilot_episode_count(production: AgentProduction) -> int:
    spec = production.production_spec or {}
    if int(spec.get("workflow_version") or 1) >= 2:
        return max(0, int(spec.get("pilot_episode_count") or 0))
    return max(1, int(spec.get("pilot_episode_count") or 1))


def uses_agent_workflow_v2(production: AgentProduction) -> bool:
    return int((production.production_spec or {}).get("workflow_version") or 1) >= 2


def bump_script_review_version(
    production: AgentProduction,
    step: AgentStep,
    checkpoint: Optional[AgentCheckpoint],
) -> Optional[int]:
    if (
        not uses_agent_workflow_v2(production)
        or checkpoint is None
        or checkpoint.checkpoint_type != "script_review"
    ):
        return None
    version = max(1, int(step.output_version or 1)) + 1
    step.output_version = version
    checkpoint.impact = {
        **(checkpoint.impact or {}),
        "script_version": version,
    }
    return version
