from types import SimpleNamespace

from app.main import app
from app.services.agent_workflow_steps import (
    PRODUCT_STEP_CODES,
    _episode_script_status,
    _episode_storyboard_status,
    _storyboard_generation_status,
    _video_editing_status,
    current_product_step_number,
)


def test_agent_workflow_route_is_registered() -> None:
    routes = {
        (path, method.upper())
        for path, methods in app.openapi()["paths"].items()
        for method in methods
    }

    assert ("/api/v1/agent-productions/{production_id}/workflow", "GET") in routes


def test_agent_product_workflow_has_exactly_four_steps() -> None:
    assert PRODUCT_STEP_CODES == {
        1: "script_processing",
        2: "asset_confirmation",
        3: "storyboard_generation",
        4: "video_editing",
    }


def test_materialized_agent_chapter_completes_episode_script_step() -> None:
    chapter = SimpleNamespace(
        process_status="success",
        processed_content="第一集剧情",
    )

    assert _episode_script_status(chapter) == "completed"


def test_current_step_is_first_incomplete_step() -> None:
    assert current_product_step_number(
        {1: "completed", 2: "waiting_review", 3: "not_started", 4: "not_started"}
    ) == 2
    assert current_product_step_number(
        {1: "completed", 2: "completed", 3: "invalidated", 4: "not_started"}
    ) == 3
    assert current_product_step_number(
        {1: "completed", 2: "completed", 3: "completed", 4: "completed"}
    ) == 4


def test_storyboard_step_completes_after_analysis_without_video() -> None:
    chapter = SimpleNamespace(extra={"storyboard_analysis_status": "success"})
    storyboard = SimpleNamespace(extra={"video_generation_status": "not_started"})

    assert _episode_storyboard_status(chapter, [storyboard], "completed") == "completed"

    storyboard.extra = {"video_generation_status": "selection_required"}
    assert _episode_storyboard_status(chapter, [storyboard], "completed") == "completed"


def test_completed_storyboard_analysis_opens_video_editing_step() -> None:
    production = SimpleNamespace(current_stage="episode_videos")

    step_three = _storyboard_generation_status(
        production,
        "completed",
        None,
        ["completed", "completed"],
    )
    step_four = _video_editing_status(step_three, False)

    assert step_three == "completed"
    assert step_four == "waiting_review"
    assert current_product_step_number(
        {1: "completed", 2: "completed", 3: step_three, 4: step_four}
    ) == 4


def test_one_completed_episode_opens_review_while_other_episodes_are_processing() -> None:
    assert _video_editing_status(
        "processing",
        False,
        ["completed", "processing"],
    ) == "waiting_review"
