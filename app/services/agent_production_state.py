from typing import Dict, FrozenSet


class InvalidAgentStateTransition(ValueError):
    def __init__(self, machine: str, current: str, target: str) -> None:
        self.machine = machine
        self.current = current
        self.target = target
        super().__init__(f"invalid {machine} state transition: {current} -> {target}")


PRODUCTION_STATUSES: FrozenSet[str] = frozenset(
    {
        "draft",
        "planning",
        "waiting_approval",
        "running",
        "paused",
        "partially_failed",
        "completed",
        "cancelled",
    }
)

STEP_STATUSES: FrozenSet[str] = frozenset(
    {
        "not_started",
        "queued",
        "running",
        "waiting_approval",
        "completed",
        "failed",
        "invalidated",
        "skipped",
    }
)

CHECKPOINT_STATUSES: FrozenSet[str] = frozenset({"pending", "approved", "rejected"})

_PRODUCTION_TRANSITIONS: Dict[str, FrozenSet[str]] = {
    "draft": frozenset({"planning", "cancelled"}),
    "planning": frozenset(
        {"waiting_approval", "running", "partially_failed", "paused", "cancelled"}
    ),
    "waiting_approval": frozenset({"planning", "running", "cancelled"}),
    "running": frozenset(
        {"waiting_approval", "paused", "partially_failed", "completed", "cancelled"}
    ),
    "paused": frozenset({"running", "cancelled"}),
    "partially_failed": frozenset({"running", "paused", "completed", "cancelled"}),
    "completed": frozenset({"planning"}),
    "cancelled": frozenset(),
}

_STEP_TRANSITIONS: Dict[str, FrozenSet[str]] = {
    "not_started": frozenset({"queued", "skipped"}),
    "queued": frozenset({"running", "failed", "skipped"}),
    "running": frozenset({"waiting_approval", "completed", "failed", "skipped"}),
    "waiting_approval": frozenset({"running", "completed", "failed", "invalidated", "skipped"}),
    "completed": frozenset({"invalidated"}),
    "failed": frozenset({"queued", "skipped"}),
    "invalidated": frozenset({"queued", "skipped"}),
    "skipped": frozenset(),
}

_CHECKPOINT_TRANSITIONS: Dict[str, FrozenSet[str]] = {
    "pending": frozenset({"approved", "rejected"}),
    "approved": frozenset(),
    "rejected": frozenset(),
}


def transition_production_status(current: str, target: str) -> str:
    return _transition("production", PRODUCTION_STATUSES, _PRODUCTION_TRANSITIONS, current, target)


def transition_step_status(current: str, target: str) -> str:
    return _transition("step", STEP_STATUSES, _STEP_TRANSITIONS, current, target)


def transition_checkpoint_status(current: str, target: str) -> str:
    return _transition(
        "checkpoint",
        CHECKPOINT_STATUSES,
        _CHECKPOINT_TRANSITIONS,
        current,
        target,
    )


def _transition(
    machine: str,
    statuses: FrozenSet[str],
    transitions: Dict[str, FrozenSet[str]],
    current: str,
    target: str,
) -> str:
    if current not in statuses or target not in statuses:
        raise InvalidAgentStateTransition(machine, current, target)
    if current == target:
        return target
    if target not in transitions[current]:
        raise InvalidAgentStateTransition(machine, current, target)
    return target
