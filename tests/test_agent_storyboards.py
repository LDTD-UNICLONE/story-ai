import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.main import app
from app.schemas.agent_storyboard import (
    AgentStoryboardCopyRequest,
    AgentStoryboardCreateRequest,
    AgentStoryboardGenerateRequest,
    AgentStoryboardReorderRequest,
    AgentStoryboardUpdateRequest,
)
from app.services.agent.batch_productions import (
    _eligible_scope_ids,
    _scoped_eligible_scope_ids,
)
from app.services.agent.storyboard_bindings import _preserve_manual_bindings
from app.services.agent.storyboard_episode_state import (
    build_storyboard_episode_progress,
)
from app.services.agent.storyboards import storyboard_estimated_duration_seconds
from app.services.agent.storyboard_prompts import (
    asset_token_keys,
    render_asset_tokens,
    tokenize_asset_text,
    tokenize_storyboard_shots,
)
from app.services.projects.storyboard_images import _build_storyboard_image_prompt
from app.services.projects.storyboard_videos import _build_storyboard_reference_video_prompt
from app.services.prompts import render_system_prompt
from app.services.projects.storyboard_parsing import (
    validate_agent_storyboard_sequence,
    build_agent_storyboard_prompt,
    estimate_agent_shot_group_duration,
    parse_storyboard_items,
)
from app.services.projects.storyboard_execution import (
    storyboard_analysis_task_is_current,
)
from app.core.exceptions import AppException


def test_agent_storyboard_routes_are_registered() -> None:
    routes = {
        (path, method.upper())
        for path, methods in app.openapi()["paths"].items()
        for method in methods
    }
    prefix = "/api/v1/agent-productions/{production_id}/storyboards"
    assert (prefix, "GET") in routes
    assert (f"{prefix}/generations", "POST") in routes
    assert (f"{prefix}/{{storyboard_id}}", "PATCH") in routes
    assert (f"{prefix}/{{storyboard_id}}", "DELETE") in routes
    assert (f"{prefix}/{{storyboard_id}}/copy", "POST") in routes
    episode_prefix = "/api/v1/agent-productions/{production_id}/episodes/{chapter_id}"
    assert (f"{episode_prefix}/storyboards", "POST") in routes
    assert (f"{episode_prefix}/storyboards/order", "PUT") in routes


def test_storyboard_generate_request_normalizes_idempotency_key() -> None:
    payload = AgentStoryboardGenerateRequest(
        expected_core_asset_lock_version=2,
        idempotency_key="  storyboard-generation-v2  ",
    )
    assert payload.idempotency_key == "storyboard-generation-v2"

    with pytest.raises(ValidationError):
        AgentStoryboardGenerateRequest(
            expected_core_asset_lock_version=0,
            idempotency_key="short",
        )


def test_storyboard_update_replaces_bindings_and_requires_fixed_prompt_rules() -> None:
    character_id = uuid4()
    payload = AgentStoryboardUpdateRequest(
        expected_core_asset_lock_version=2,
        expected_revision=1,
        storyboard_prompt=(
            "画面风格：写实漫剧\n"
            "视频中不得出现任何字幕、文字叠加、纯画面，不要BGM，不要配乐。\n"
            "镜头1：人物走入旧宅"
        ),
        asset_bindings=[
            {
                "asset_type": "character",
                "asset_id": character_id,
                "variant_id": uuid4(),
            }
        ],
    )
    assert payload.asset_bindings[0].asset_id == character_id

    duration_update = AgentStoryboardUpdateRequest(
        expected_core_asset_lock_version=2,
        expected_revision=1,
        estimated_duration_seconds=12,
    )
    assert duration_update.estimated_duration_seconds == 12

    with pytest.raises(ValidationError):
        AgentStoryboardUpdateRequest(
            expected_core_asset_lock_version=2,
            expected_revision=1,
            estimated_duration_seconds=16,
        )

    with pytest.raises(ValidationError):
        AgentStoryboardUpdateRequest(
            expected_core_asset_lock_version=2,
            expected_revision=1,
        )

    with pytest.raises(ValidationError):
        AgentStoryboardUpdateRequest(
            expected_core_asset_lock_version=2,
            prompt_notes="缺少并发版本",
        )


def test_storyboard_edit_requests_support_revision_and_group_operations() -> None:
    chapter_id = uuid4()
    storyboard_id = uuid4()
    create = AgentStoryboardCreateRequest(
        expected_core_asset_lock_version=2,
        expected_episode_revision=3,
        insert_after_storyboard_id=storyboard_id,
    )
    assert create.title == "新分镜组"

    copied = AgentStoryboardCopyRequest(
        expected_core_asset_lock_version=2,
        expected_revision=4,
    )
    assert copied.expected_revision == 4

    reordered = AgentStoryboardReorderRequest(
        expected_episode_revision=3,
        storyboard_ids=[storyboard_id, chapter_id],
    )
    assert reordered.storyboard_ids == [storyboard_id, chapter_id]

    with pytest.raises(ValidationError):
        AgentStoryboardReorderRequest(
            expected_episode_revision=3,
            storyboard_ids=[storyboard_id, storyboard_id],
        )


def test_asset_tokens_keep_prompt_linked_when_binding_label_changes() -> None:
    template = tokenize_asset_text(
        "沈砚走进旧宅，沈砚看见玉佩。",
        {"沈砚": "character_1", "旧宅": "scene_1", "玉佩": "prop_1"},
    )

    assert template == (
        "{{asset:character_1}}走进{{asset:scene_1}}，"
        "{{asset:character_1}}看见{{asset:prop_1}}。"
    )
    assert render_asset_tokens(
        template,
        {
            "character_1": "沈砚中年造型",
            "scene_1": "废弃旧宅",
            "prop_1": "染血玉佩",
        },
    ) == "沈砚中年造型走进废弃旧宅，沈砚中年造型看见染血玉佩。"

    shots = tokenize_storyboard_shots(
        [
            {
                "characters": ["沈砚"],
                "scene_name": "旧宅",
                "props": ["玉佩"],
                "visual_content": "沈砚在旧宅拿起玉佩",
            }
        ],
        {"沈砚": "character_1", "旧宅": "scene_1", "玉佩": "prop_1"},
    )
    assert shots[0]["character_binding_keys"] == ["character_1"]
    assert shots[0]["scene_binding_key"] == "scene_1"
    assert shots[0]["prop_binding_keys"] == ["prop_1"]


def test_asset_token_keys_follow_first_mention_order_without_duplicates() -> None:
    assert asset_token_keys(
        "{{asset:scene_1}}中，{{asset:character_1}}拿起{{asset:prop_1}}，"
        "随后{{asset:character_1}}离开。"
    ) == ["scene_1", "character_1", "prop_1"]


def test_manual_storyboard_bindings_survive_same_core_lock_refresh() -> None:
    storyboard = SimpleNamespace(
        extra={
            "agent_asset_binding_source": "user",
            "agent_core_asset_lock_id": str(uuid4()),
            "agent_core_asset_lock_version": 3,
        }
    )
    lock = SimpleNamespace(
        id=storyboard.extra["agent_core_asset_lock_id"],
        version=3,
    )

    assert _preserve_manual_bindings(storyboard, lock) is True
    lock.version = 4
    assert _preserve_manual_bindings(storyboard, lock) is False


def test_agent_storyboard_parser_preserves_script_fields_and_duration() -> None:
    items = parse_storyboard_items(
        json.dumps(
            {
                "storyboard_units": [
                    {
                        "shot_number": 9,
                        "title": "沈砚推门入宅",
                        "source_content": "沈砚推开旧宅木门。",
                        "event_goal": "主角进入关键场景",
                        "scene_name": "旧宅",
                        "scene_state": "夜晚，室内昏暗",
                        "characters": ["沈砚"],
                        "props": ["木门"],
                        "action": "沈砚推开木门并警惕观察",
                        "shot_size": "中景",
                        "camera_angle": "平视",
                        "camera_movement": "缓慢推进",
                        "screen_execution": "木门开启，沈砚进入画面",
                        "character_action": "推门后停步观察",
                        "character_expression": "警惕",
                        "dialogue": "",
                        "sound_effect": "木门吱呀声",
                        "atmosphere": "压抑",
                        "duration_seconds": 6,
                        "production_focus": "保持人物和旧宅空间连续",
                        "ending_frame": "沈砚望向屋内",
                    }
                ]
            },
            ensure_ascii=False,
        )
    )

    assert len(items) == 1
    item = items[0]
    assert item["shot_size"] == "中景"
    assert item["camera_movement"] == "缓慢推进"
    assert item["screen_execution"] == "木门开启，沈砚进入画面"
    assert item["duration_suggestion"] == "6秒"
    assert item["estimated_duration_seconds"] == 6


def test_storyboard_duration_prefers_structured_seconds() -> None:
    storyboard = SimpleNamespace(
        duration_suggestion="约 8 秒",
        extra={"estimated_duration_seconds": 6},
    )
    assert storyboard_estimated_duration_seconds(storyboard) == 6

    legacy = SimpleNamespace(duration_suggestion="约 8 秒", extra={})
    assert storyboard_estimated_duration_seconds(legacy) == 8


def test_agent_storyboard_sequence_requires_ordered_source_coverage() -> None:
    items = [
        {"shot_number": 1, "source_content": "沈砚进入旧宅。"},
        {"shot_number": 2, "source_content": "他发现桌上的玉佩。"},
    ]
    validate_agent_storyboard_sequence(
        items,
        "沈砚进入旧宅。\n他发现桌上的玉佩。",
    )

    with pytest.raises(AppException, match="序号不连续"):
        validate_agent_storyboard_sequence(
            [{**items[0], "shot_number": 2}, items[1]],
            "沈砚进入旧宅。他发现桌上的玉佩。",
        )

    with pytest.raises(AppException, match="未覆盖"):
        validate_agent_storyboard_sequence(
            [items[0]],
            "沈砚进入旧宅。他发现桌上的玉佩。随后黑衣人现身。",
        )


def test_storyboard_generation_selects_all_pending_episodes() -> None:
    chapters = [
        SimpleNamespace(id=uuid4(), extra={"storyboard_analysis_status": "not_started"}),
        SimpleNamespace(id=uuid4(), extra={"storyboard_analysis_status": "not_started"}),
        SimpleNamespace(id=uuid4(), extra={"storyboard_analysis_status": "not_started"}),
    ]
    context = SimpleNamespace(
        step=SimpleNamespace(extra={"storyboard_attempts": {}}),
        production=SimpleNamespace(production_spec={"workflow_version": 2}),
        chapters=chapters,
        storyboards=[],
    )

    assert _eligible_scope_ids(context, "storyboards") == [
        chapter.id for chapter in chapters
    ]


def test_storyboard_generation_does_not_block_other_episodes() -> None:
    active_chapters = [
        SimpleNamespace(id=uuid4(), extra={"storyboard_analysis_status": "success"}),
        SimpleNamespace(id=uuid4(), extra={"storyboard_analysis_status": "running"}),
        SimpleNamespace(id=uuid4(), extra={"storyboard_analysis_status": "not_started"}),
    ]
    context = SimpleNamespace(
        step=SimpleNamespace(extra={"storyboard_attempts": {}}),
        production=SimpleNamespace(production_spec={"workflow_version": 2}),
        chapters=active_chapters,
        storyboards=[],
    )
    assert _eligible_scope_ids(context, "storyboards") == [active_chapters[2].id]

    active_chapters[1].extra["storyboard_analysis_status"] = "failed"
    assert _eligible_scope_ids(context, "storyboards") == [active_chapters[2].id]


def test_incremental_storyboard_generation_only_selects_supplement_episodes() -> None:
    original = SimpleNamespace(
        id=uuid4(),
        extra={"storyboard_analysis_status": "not_started"},
    )
    supplement = SimpleNamespace(
        id=uuid4(),
        extra={"storyboard_analysis_status": "not_started"},
    )
    context = SimpleNamespace(
        step=SimpleNamespace(extra={"storyboard_attempts": {}}),
        production=SimpleNamespace(production_spec={"workflow_version": 2}),
        chapters=[original, supplement],
        storyboards=[],
    )

    assert _scoped_eligible_scope_ids(
        context,
        "storyboards",
        [supplement.id],
    ) == [supplement.id]


def test_persisted_storyboard_scope_survives_controller_dispatches() -> None:
    original = SimpleNamespace(
        id=uuid4(),
        extra={"storyboard_analysis_status": "not_started"},
    )
    supplement = SimpleNamespace(
        id=uuid4(),
        extra={"storyboard_analysis_status": "not_started"},
    )
    context = SimpleNamespace(
        step=SimpleNamespace(
            extra={
                "storyboard_attempts": {},
                "storyboard_scope_ids": [str(supplement.id)],
            }
        ),
        production=SimpleNamespace(production_spec={"workflow_version": 2}),
        chapters=[original, supplement],
        storyboards=[],
    )

    assert _eligible_scope_ids(context, "storyboards") == [supplement.id]


def test_storyboard_progress_exposes_every_completed_episode() -> None:
    chapters = [
        SimpleNamespace(
            id=uuid4(),
            extra={"storyboard_analysis_status": "success"},
        ),
        SimpleNamespace(
            id=uuid4(),
            extra={"storyboard_analysis_status": "running"},
        ),
        SimpleNamespace(
            id=uuid4(),
            extra={"storyboard_analysis_status": "success"},
        ),
    ]
    progress = build_storyboard_episode_progress(
        chapters,
        {chapters[0].id: 2, chapters[2].id: 3},
    )

    assert progress.visible_chapter_ids == (chapters[0].id, chapters[2].id)
    assert progress.current_chapter_id == chapters[1].id
    assert progress.current_status == "running"
    assert progress.completed_episode_count == 2
    assert progress.remaining_episode_count == 1
    assert progress.analysis_complete is False


def test_storyboard_progress_hides_result_with_stale_input_fingerprint() -> None:
    chapter = SimpleNamespace(
        id=uuid4(),
        extra={
            "storyboard_analysis_status": "success",
            "storyboard_analysis_input_fingerprint": "new-input",
            "storyboard_analysis_result_fingerprint": "old-input",
        },
    )
    progress = build_storyboard_episode_progress([chapter], {chapter.id: 1})

    assert progress.visible_chapter_ids == ()
    assert progress.current_chapter_id == chapter.id
    assert progress.current_status == "stale"


def test_agent_storyboard_task_must_match_latest_chapter_input() -> None:
    task_id = uuid4()
    task = SimpleNamespace(
        id=task_id,
        extra={
            "agent_production_id": str(uuid4()),
            "storyboard_analysis_input_fingerprint": "input-v2",
        },
    )
    chapter = SimpleNamespace(
        extra={
            "storyboard_analysis_task_record_id": str(task_id),
            "storyboard_analysis_input_fingerprint": "input-v2",
        }
    )
    assert storyboard_analysis_task_is_current(task, chapter) is True

    chapter.extra["storyboard_analysis_input_fingerprint"] = "input-v3"
    assert storyboard_analysis_task_is_current(task, chapter) is False


def test_agent_storyboard_group_aggregates_shots_assets_and_dialogue_duration() -> None:
    items = parse_storyboard_items(
        json.dumps(
            {
                "storyboard_groups": [
                    {
                        "group_number": 7,
                        "title": "沈砚入宅发现玉佩",
                        "source_content": "沈砚推门入宅，说道：果然在这里。他看见桌上的玉佩。",
                        "event_goal": "进入场景并发现线索",
                        "shots": [
                            {
                                "shot_number": 4,
                                "shot_size": "全景",
                                "camera_shot": "从门外拍摄沈砚入宅",
                                "camera_angle": "平视",
                                "camera_movement": "缓慢推进",
                                "visual_content": "沈砚推开旧宅木门进入室内",
                                "scene_name": "旧宅",
                                "characters": ["沈砚"],
                                "props": ["木门"],
                                "speaker": "沈砚",
                                "dialogue": "果然在这里",
                            },
                            {
                                "shot_number": 9,
                                "shot_size": "特写",
                                "camera_shot": "拍摄桌面上的玉佩",
                                "camera_angle": "俯拍",
                                "camera_movement": "固定",
                                "visual_content": "玉佩躺在积灰的桌面上",
                                "scene_name": "旧宅",
                                "characters": [],
                                "props": ["玉佩"],
                                "speaker": "",
                                "dialogue": "",
                            },
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        )
    )

    assert len(items) == 1
    group = items[0]
    assert [shot["shot_number"] for shot in group["shots"]] == [1, 2]
    assert group["characters"] == ["沈砚"]
    assert group["props"] == ["木门", "玉佩"]
    assert group["scene_name"] == "旧宅"
    assert group["estimated_duration_seconds"] == 4


def test_agent_storyboard_duration_uses_four_characters_or_words_per_second() -> None:
    chinese = [{"dialogue": "一二三四五六七八", "visual_content": "人物说话"}]
    english = [
        {
            "dialogue": "one two three four five six seven eight nine ten eleven twelve "
            "thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty",
            "visual_content": "The character speaks",
        }
    ]
    too_long = [{"dialogue": "字" * 64, "visual_content": "人物持续说话"}]

    assert estimate_agent_shot_group_duration(chinese) == 4
    assert estimate_agent_shot_group_duration(english) == 5
    assert estimate_agent_shot_group_duration(too_long) == 16


def test_agent_storyboard_prompt_is_built_from_fixed_rules_and_shot_group() -> None:
    prompt = build_agent_storyboard_prompt(
        "写实国风漫剧，电影级光影",
        [
            {
                "shot_number": 1,
                "shot_size": "近景",
                "camera_shot": "拍摄沈砚正面",
                "camera_angle": "平视",
                "camera_movement": "缓慢推进",
                "visual_content": "沈砚注视桌上的玉佩",
                "speaker": "沈砚",
                "dialogue": "终于找到了",
            },
            {
                "shot_number": 2,
                "shot_size": "特写",
                "camera_shot": "拍摄玉佩细节",
                "camera_angle": "俯拍",
                "camera_movement": "固定",
                "visual_content": "玉佩表面泛起微光",
                "speaker": "",
                "dialogue": "",
            },
        ],
        6,
    )

    assert prompt.startswith("画面风格：写实国风漫剧，电影级光影")
    assert "视频中不得出现任何字幕、文字叠加" in prompt
    assert "纯画面" in prompt
    assert "不要BGM，不要配乐" in prompt
    assert "镜头1：景别：近景" in prompt
    assert "人物说台词：沈砚：终于找到了" in prompt
    assert "镜头2：景别：特写" in prompt
    assert "镜头2：" in prompt and "镜头2：景别：特写" in prompt
    assert prompt.endswith("分镜组总时长：6秒")


def test_agent_video_uses_group_prompt_without_project_storyboard_template() -> None:
    storyboard = SimpleNamespace(
        extra={"agent_storyboard_prompt": "画面风格：写实漫剧\n镜头1：纯画面"}
    )

    prompt = _build_storyboard_reference_video_prompt(
        None,
        storyboard,
        None,
        {},
    )

    assert prompt == "画面风格：写实漫剧\n镜头1：纯画面"
    assert "请根据当前分镜的故事版参考图" not in prompt


def test_selected_asset_variant_is_injected_into_agent_image_and_video_prompts() -> None:
    variant_context = [
        {
            "name": "沈砚中年造型",
            "description": "鬓角斑白",
            "trigger_reason": "十年后",
        }
    ]
    storyboard = SimpleNamespace(
        title="十年后重逢",
        image_prompt="沈砚走入旧宅",
        screen_execution=None,
        action="走入旧宅",
        source_content="十年后，沈砚重返旧宅。",
        characters=["沈砚"],
        scene_name="旧宅",
        props=[],
        negative_prompt=None,
        video_prompt=None,
        dialogue=None,
        duration_suggestion="6秒",
        extra={
            "agent_storyboard_prompt": "画面风格：写实漫剧\n镜头1：沈砚走入旧宅",
            "agent_asset_variant_context": variant_context,
        },
    )
    project = SimpleNamespace(style=SimpleNamespace(prompt="写实漫剧"))

    image_prompt = _build_storyboard_image_prompt(project, storyboard, [])
    video_prompt = _build_storyboard_reference_video_prompt(project, storyboard, None, {})

    expected = "沈砚中年造型（鬓角斑白，触发：十年后）"
    assert expected in image_prompt
    assert expected in video_prompt


def test_agent_storyboard_analysis_proactively_splits_long_dialogue_for_editing() -> None:
    prompt = render_system_prompt(
        "agent_storyboard_generation.md",
        visual_style="写实漫剧",
        input_text="角色进行一段较长的陈述。",
        characters="[]",
        scenes="[]",
        props="[]",
    )

    assert "输出前先估算" in prompt
    assert "超过 15 秒" in prompt
    assert "主动拆分" in prompt
    assert "自然剪辑点" in prompt
    assert "听者的表情和反应" in prompt
    assert "环境建立镜头" in prompt
    assert "关键道具或空间细节" in prompt
    assert "画外音继续" in prompt
    assert "不得连续使用单一的人物正面说话镜头" in prompt
