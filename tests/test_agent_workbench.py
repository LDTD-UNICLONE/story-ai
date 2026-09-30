from app.services.agent.workbench import resolve_workbench_actions


def test_draft_workbench_can_start() -> None:
    next_action, actions = resolve_workbench_actions("draft", "source")

    assert next_action == "start"
    assert actions == ["start", "cancel"]


def test_unconfigured_agent_draft_requires_configuration() -> None:
    next_action, actions = resolve_workbench_actions(
        "draft",
        "source",
        configuration_confirmed=False,
    )

    assert next_action == "configure"
    assert actions == ["configure", "cancel"]


def test_waiting_episode_plan_requires_review() -> None:
    next_action, actions = resolve_workbench_actions(
        "waiting_approval",
        "episode_plan_review",
    )

    assert next_action == "review_episode_plan"
    assert actions == ["review_episode_plan", "cancel"]


def test_active_batch_can_be_paused() -> None:
    next_action, actions = resolve_workbench_actions("running", "batch_videos")

    assert next_action == "monitor_batch"
    assert actions == ["monitor_batch", "pause", "cancel"]


def test_core_assets_flow_enters_storyboard_step() -> None:
    assert resolve_workbench_actions("planning", "batch_production") == (
        "generate_storyboards",
        ["generate_storyboards", "pause", "cancel"],
    )
    assert resolve_workbench_actions("running", "batch_storyboards") == (
        "monitor_storyboards",
        ["monitor_storyboards", "pause", "cancel"],
    )
    assert resolve_workbench_actions("running", "episode_videos") == (
        "generate_episode_videos",
        ["generate_episode_videos", "pause", "cancel"],
    )


def test_terminal_workbench_has_no_mutating_actions() -> None:
    assert resolve_workbench_actions("completed", "completed") == (
        "review_results",
        ["review_results"],
    )
    assert resolve_workbench_actions("cancelled", "batch_images") == (None, [])
