import pytest

from app.services.agent.production_state import (
    InvalidAgentStateTransition,
    transition_checkpoint_status,
    transition_production_status,
    transition_step_status,
)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("draft", "planning"),
        ("planning", "running"),
        ("planning", "waiting_approval"),
        ("waiting_approval", "planning"),
        ("waiting_approval", "running"),
        ("running", "paused"),
        ("paused", "running"),
        ("running", "partially_failed"),
        ("running", "cancelled"),
        ("planning", "cancelled"),
        ("partially_failed", "completed"),
        ("draft", "cancelled"),
    ],
)
def test_valid_production_transitions(current: str, target: str) -> None:
    assert transition_production_status(current, target) == target


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("not_started", "queued"),
        ("queued", "running"),
        ("running", "waiting_approval"),
        ("waiting_approval", "completed"),
        ("completed", "invalidated"),
        ("invalidated", "queued"),
        ("failed", "queued"),
        ("not_started", "skipped"),
        ("running", "skipped"),
        ("waiting_approval", "skipped"),
    ],
)
def test_valid_step_transitions(current: str, target: str) -> None:
    assert transition_step_status(current, target) == target


@pytest.mark.parametrize("target", ["approved", "rejected"])
def test_pending_checkpoint_can_be_resolved(target: str) -> None:
    assert transition_checkpoint_status("pending", target) == target


@pytest.mark.parametrize(
    ("transition", "status"),
    [
        (transition_production_status, "running"),
        (transition_step_status, "queued"),
        (transition_checkpoint_status, "pending"),
    ],
)
def test_repeating_a_known_state_is_idempotent(transition, status: str) -> None:
    assert transition(status, status) == status


@pytest.mark.parametrize(
    ("transition", "current", "target"),
    [
        (transition_production_status, "completed", "running"),
        (transition_production_status, "draft", "completed"),
        (transition_step_status, "completed", "running"),
        (transition_step_status, "skipped", "queued"),
        (transition_checkpoint_status, "approved", "rejected"),
    ],
)
def test_invalid_transitions_are_rejected(transition, current: str, target: str) -> None:
    with pytest.raises(InvalidAgentStateTransition):
        transition(current, target)


def test_unknown_state_is_rejected() -> None:
    with pytest.raises(InvalidAgentStateTransition):
        transition_production_status("unknown", "running")
