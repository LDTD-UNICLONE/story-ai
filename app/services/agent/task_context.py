from typing import Dict
from uuid import UUID


def build_agent_task_context(
    *,
    production_id: UUID,
    step_id: UUID,
    stage: str,
    scope_type: str,
    scope_id: UUID,
    attempt_number: int,
) -> Dict[str, object]:
    return {
        "agent_production_id": str(production_id),
        "agent_step_id": str(step_id),
        "agent_stage": stage,
        "agent_scope_type": scope_type,
        "agent_scope_id": str(scope_id),
        "agent_attempt_number": max(1, attempt_number),
    }
