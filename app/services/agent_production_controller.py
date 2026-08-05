from datetime import timedelta
from typing import List, Optional
from uuid import UUID, uuid4

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.timezone import beijing_datetime
from app.models.agent_production import (
    AgentControllerState,
    AgentEvent,
    AgentProduction,
    AgentStep,
)
from app.models.user import User
from app.schemas.agent_batch_production import AgentBatchDispatchRequest
from app.services.agent_batch_productions import (
    dispatch_batch_production,
    get_batch_production,
)
from app.services.agent_workflow import uses_agent_workflow_v2


CONTROLLER_PRODUCTION_STATUSES = ("planning", "running", "partially_failed")
QUEUE_LEASE_SECONDS = 60
FAILURE_BACKOFF_SECONDS = 60


async def list_agent_controller_candidates(
    db: AsyncSession,
    *,
    limit: int = 100,
) -> List[UUID]:
    batch_step_exists = exists(
        select(AgentStep.id).where(
            AgentStep.production_id == AgentProduction.id,
            AgentStep.stage == "batch_production",
        )
    )
    result = await db.execute(
        select(AgentProduction.id)
        .where(
            AgentProduction.status.in_(CONTROLLER_PRODUCTION_STATUSES),
            or_(
                AgentProduction.mode == "automatic",
                and_(
                    func.coalesce(
                        AgentProduction.production_spec["workflow_version"].as_integer(),
                        1,
                    )
                    >= 2,
                    AgentProduction.current_stage.in_(
                        ("batch_production", "batch_storyboards")
                    ),
                ),
            ),
            or_(
                AgentProduction.current_stage == "batch_production",
                and_(
                    AgentProduction.current_stage.like("batch_%"),
                    batch_step_exists,
                ),
            ),
        )
        .order_by(AgentProduction.updated_at, AgentProduction.id)
        .limit(max(1, limit))
    )
    return list(result.scalars().all())


async def advance_agent_batch_production(
    db: AsyncSession,
    production_id: UUID,
) -> bool:
    result = await db.execute(
        select(AgentProduction, User)
        .join(User, User.id == AgentProduction.user_id)
        .where(AgentProduction.id == production_id)
    )
    row = result.one_or_none()
    if row is None:
        return False
    production, user = row
    supervised_storyboard_start = (
        production.mode == "supervised"
        and uses_agent_workflow_v2(production)
        and production.current_stage in {"batch_production", "batch_storyboards"}
    )
    if production.mode != "automatic" and not supervised_storyboard_start:
        return False
    status = await get_batch_production(db, production.id, user.id)
    if not _should_dispatch(status):
        return False

    await db.refresh(production)
    await dispatch_batch_production(
        db,
        production.id,
        user,
        AgentBatchDispatchRequest(
            expected_core_asset_lock_version=int(status["core_asset_lock_version"]),
            idempotency_key=f"agent-controller-{production.id}-{production.lock_version}",
            max_tasks=int(status["recommended_batch_size"]),
        ),
        dispatch_source="system",
    )
    return True


async def queue_agent_controller_claim(
    db: AsyncSession,
    production_id: UUID,
) -> Optional[UUID]:
    await db.execute(
        insert(AgentControllerState)
        .values(production_id=production_id, status="idle", last_result={})
        .on_conflict_do_nothing(index_elements=[AgentControllerState.production_id])
    )
    state = await _locked_controller_state(db, production_id)
    if state is None:
        await db.commit()
        return None
    now = beijing_datetime()
    if state.status in {"queued", "running", "failed"} and _lease_is_active(state, now):
        await db.commit()
        return None
    token = uuid4()
    state.status = "queued"
    state.lease_token = token
    state.lease_expires_at = now + timedelta(seconds=QUEUE_LEASE_SECONDS)
    await db.commit()
    return token


async def start_agent_controller_claim(
    db: AsyncSession,
    production_id: UUID,
    lease_token: UUID,
) -> bool:
    state = await _locked_controller_state(db, production_id)
    now = beijing_datetime()
    if (
        state is None
        or state.status != "queued"
        or state.lease_token != lease_token
        or not _lease_is_active(state, now)
    ):
        await db.commit()
        return False
    state.status = "running"
    state.attempt_count += 1
    state.last_started_at = now
    state.last_error = None
    state.lease_expires_at = now + timedelta(seconds=_running_lease_seconds())
    await db.commit()
    return True


async def finish_agent_controller_claim(
    db: AsyncSession,
    production_id: UUID,
    lease_token: UUID,
    *,
    advanced: bool,
) -> bool:
    state = await _locked_controller_state(db, production_id)
    if not _owns_running_claim(state, lease_token):
        await db.commit()
        return False
    state.status = "idle"
    state.lease_token = None
    state.lease_expires_at = None
    state.last_finished_at = beijing_datetime()
    state.last_error = None
    state.last_result = {"advanced": advanced}
    await db.commit()
    return True


async def fail_agent_controller_claim(
    db: AsyncSession,
    production_id: UUID,
    lease_token: UUID,
    error: str,
) -> bool:
    state = await _locked_controller_state(db, production_id)
    if not _owns_running_claim(state, lease_token):
        await db.commit()
        return False
    now = beijing_datetime()
    public_error = (error.strip() or "Agent 控制器执行失败")[:2000]
    state.status = "failed"
    state.lease_expires_at = now + timedelta(seconds=FAILURE_BACKOFF_SECONDS)
    state.last_finished_at = now
    state.last_error = public_error
    state.last_result = {"advanced": False}
    db.add(
        AgentEvent(
            production_id=production_id,
            event_type="controller.failed",
            source="system",
            payload={
                "controller_state_id": str(state.id),
                "attempt_count": state.attempt_count,
                "error": public_error,
            },
        )
    )
    await db.commit()
    return True


def _should_dispatch(status: dict) -> bool:
    return bool(
        status.get("can_dispatch")
        and not status.get("paused")
        and not status.get("is_complete")
        and int(status.get("recommended_batch_size") or 0) > 0
    )


async def _locked_controller_state(
    db: AsyncSession,
    production_id: UUID,
) -> Optional[AgentControllerState]:
    result = await db.execute(
        select(AgentControllerState)
        .where(AgentControllerState.production_id == production_id)
        .with_for_update()
    )
    return result.scalar_one_or_none()


def _lease_is_active(state: AgentControllerState, now) -> bool:
    return state.lease_expires_at is not None and state.lease_expires_at > now


def _owns_running_claim(
    state: Optional[AgentControllerState],
    lease_token: UUID,
) -> bool:
    return bool(
        state is not None and state.status == "running" and state.lease_token == lease_token
    )


def _running_lease_seconds() -> int:
    return max(
        QUEUE_LEASE_SECONDS,
        settings.effective_celery_task_time_limit_seconds
        + settings.celery_task_timeout_grace_seconds,
    )
