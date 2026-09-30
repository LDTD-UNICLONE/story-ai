from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.core.exceptions import AppException
from app.main import app
from app.models.agent_storyboard_media import AgentStoryboardMediaRequest
from app.schemas.agent_storyboard_media import (
    AgentEpisodeVideoGenerationRequest,
    AgentStoryboardPrimaryVideoRequest,
    AgentStoryboardVideoConfigRequest,
    AgentStoryboardVideoGenerationRequest,
)
from app.services.agent.storyboard_media import (
    EPISODE_BATCH_REUSED_STATUSES,
    _request_signature,
    _storyboard_request_signature,
    _storyboard_video_parameters,
    _video_item,
)
from app.services.agent.video_settings import (
    provider_video_duration,
    require_agent_video_defaults,
)
from app.services.agent.storyboard_video_inputs import (
    _compile_prompt,
    build_agent_storyboard_video_input,
)


def test_episode_batch_skips_storyboards_waiting_for_primary_selection() -> None:
    assert "selection_required" in EPISODE_BATCH_REUSED_STATUSES


def test_agent_episode_video_routes_are_registered_without_storyboard_image_route() -> None:
    routes = {
        (path, method.upper())
        for path, methods in app.openapi()["paths"].items()
        for method in methods
    }
    prefix = "/api/v1/agent-productions/{production_id}/episodes/{chapter_id}"
    assert (f"{prefix}/videos", "GET") in routes
    assert (f"{prefix}/video-generations", "POST") in routes
    assert (
        f"{prefix}/storyboards/{{storyboard_id}}/video-generations",
        "POST",
    ) in routes
    assert (f"{prefix}/storyboards/{{storyboard_id}}/asset-options", "GET") in routes
    assert (f"{prefix}/storyboards/{{storyboard_id}}/video-config", "PUT") in routes
    assert (f"{prefix}/storyboards/{{storyboard_id}}/video-versions", "GET") in routes
    assert (f"{prefix}/storyboards/{{storyboard_id}}/primary-video", "PUT") in routes
    assert (f"{prefix}/media/image-generations", "POST") not in routes


def test_episode_video_contract_uses_episode_revision_and_has_no_storyboard_selection() -> None:
    payload = AgentEpisodeVideoGenerationRequest(
        expected_core_asset_lock_version=2,
        expected_episode_revision=3,
        idempotency_key="  episode-videos-v1  ",
        video_model_id=uuid4(),
        video_resolution="1080p",
    )
    assert payload.idempotency_key == "episode-videos-v1"
    assert payload.expected_episode_revision == 3
    assert "items" not in payload.model_dump()

    with pytest.raises(ValidationError):
        AgentEpisodeVideoGenerationRequest(
            expected_core_asset_lock_version=2,
            expected_episode_revision=0,
            idempotency_key="episode-videos-v2",
            video_model_id=uuid4(),
        )


def test_video_request_idempotency_is_scoped_by_production_and_media_type() -> None:
    constraint = next(
        item
        for item in AgentStoryboardMediaRequest.__table__.constraints
        if item.name == "uq_agent_storyboard_media_requests_key"
    )
    assert [column.name for column in constraint.columns] == [
        "production_id",
        "media_type",
        "idempotency_key",
    ]


def test_storyboard_video_contract_supports_prompt_override_and_revision() -> None:
    payload = AgentStoryboardVideoGenerationRequest(
        expected_core_asset_lock_version=2,
        expected_episode_revision=3,
        expected_storyboard_revision=4,
        expected_video_config_version=2,
        duration_seconds=9,
        idempotency_key="  storyboard-video-v1  ",
        video_model_id=uuid4(),
        video_resolution="1080p",
        prompt="  加强停顿后的眼神变化  ",
    )
    assert payload.idempotency_key == "storyboard-video-v1"
    assert payload.prompt == "加强停顿后的眼神变化"
    assert payload.expected_storyboard_revision == 4
    assert payload.expected_video_config_version == 2
    assert payload.duration_seconds == 9

    with pytest.raises(ValidationError):
        AgentStoryboardVideoGenerationRequest(
            expected_core_asset_lock_version=2,
            expected_episode_revision=3,
            expected_storyboard_revision=0,
            expected_video_config_version=0,
            duration_seconds=9,
            idempotency_key="storyboard-video-v2",
            video_model_id=uuid4(),
        )


def test_storyboard_video_config_and_primary_selection_contracts() -> None:
    config = AgentStoryboardVideoConfigRequest(
        expected_core_asset_lock_version=2,
        expected_storyboard_revision=3,
        expected_config_version=0,
        video_model_id=uuid4(),
        video_resolution="1080p",
        estimated_duration_seconds=12,
    )
    assert config.estimated_duration_seconds == 12

    with pytest.raises(ValidationError):
        AgentStoryboardVideoConfigRequest(
            expected_core_asset_lock_version=2,
            expected_storyboard_revision=3,
            expected_config_version=0,
            video_model_id=uuid4(),
            estimated_duration_seconds=16,
        )

    selection = AgentStoryboardPrimaryVideoRequest(
        expected_selection_revision=1,
        history_id=uuid4(),
    )
    assert selection.expected_selection_revision == 1


def test_automatic_mode_video_request_must_match_project_defaults() -> None:
    selected_model_id = uuid4()
    production = SimpleNamespace(
        mode="automatic",
        production_spec={
            "video_model_id": str(selected_model_id),
            "video_resolution": "1080p",
        },
    )

    require_agent_video_defaults(production, selected_model_id, "1080p")

    with pytest.raises(AppException, match="自动模式视频配置") as model_error:
        require_agent_video_defaults(production, uuid4(), "1080p")
    assert model_error.value.code == 40987

    with pytest.raises(AppException, match="自动模式视频配置") as resolution_error:
        require_agent_video_defaults(production, selected_model_id, "720p")
    assert resolution_error.value.code == 40987


def test_supervised_mode_can_select_video_settings_per_storyboard() -> None:
    production = SimpleNamespace(mode="supervised", production_spec={})

    require_agent_video_defaults(production, uuid4(), "720p")


def test_provider_managed_video_duration_is_not_sent_to_apimart() -> None:
    gemini = SimpleNamespace(
        vendor="apimart",
        model_id="gemini-omni-flash-preview",
        capabilities={},
    )
    seedance = SimpleNamespace(
        vendor="apimart",
        model_id="seedance-2.5",
        capabilities={},
    )
    other = SimpleNamespace(vendor="other", model_id="video-model", capabilities={})

    assert provider_video_duration(gemini, 8) is None
    assert provider_video_duration(seedance, 8) == 8
    assert provider_video_duration(other, 8) == 8


def test_episode_video_signature_changes_with_model_resolution_or_episode_revision() -> None:
    context = SimpleNamespace(
        production=SimpleNamespace(id=uuid4()),
        chapter=SimpleNamespace(id=uuid4()),
    )
    first = AgentEpisodeVideoGenerationRequest(
        expected_core_asset_lock_version=1,
        expected_episode_revision=2,
        idempotency_key="episode-videos-v3",
        video_model_id=uuid4(),
        video_resolution="720p",
    )
    changed_resolution = first.model_copy(update={"video_resolution": "1080p"})
    changed_episode = first.model_copy(update={"expected_episode_revision": 3})
    assert _request_signature(context, first) != _request_signature(
        context,
        changed_resolution,
    )
    assert _request_signature(context, first) != _request_signature(
        context,
        changed_episode,
    )


def test_storyboard_video_signature_includes_target_revision_and_prompt() -> None:
    context = SimpleNamespace(
        production=SimpleNamespace(id=uuid4()),
        chapter=SimpleNamespace(id=uuid4()),
    )
    storyboard_id = uuid4()
    payload = AgentStoryboardVideoGenerationRequest(
        expected_core_asset_lock_version=1,
        expected_episode_revision=2,
        expected_storyboard_revision=3,
        expected_video_config_version=1,
        duration_seconds=8,
        idempotency_key="storyboard-video-v3",
        video_model_id=uuid4(),
        prompt="保留人物侧脸",
    )
    changed_prompt = payload.model_copy(update={"prompt": "改为人物正脸"})
    changed_revision = payload.model_copy(update={"expected_storyboard_revision": 4})
    changed_duration = payload.model_copy(update={"duration_seconds": 9})
    assert _storyboard_request_signature(
        context, storyboard_id, payload
    ) != _storyboard_request_signature(context, storyboard_id, changed_prompt)
    assert _storyboard_request_signature(
        context, storyboard_id, payload
    ) != _storyboard_request_signature(context, storyboard_id, changed_revision)
    assert _storyboard_request_signature(
        context, storyboard_id, payload
    ) != _storyboard_request_signature(context, storyboard_id, changed_duration)


@pytest.mark.asyncio
async def test_single_storyboard_generation_requires_matching_video_config() -> None:
    model = SimpleNamespace(id=uuid4())
    storyboard = SimpleNamespace(
        duration_suggestion="6秒",
        extra={
            "agent_video_config": {"version": 3},
            "estimated_duration_seconds": 6,
        },
    )
    selected_model, resolution, duration, config_version = (
        await _storyboard_video_parameters(
            None,
            storyboard,
            model,
            "720p",
            use_saved_config=False,
            model_cache={model.id: model},
            requested_duration_seconds=10,
            expected_config_version=3,
        )
    )
    assert selected_model is model
    assert resolution == "720p"
    assert duration == 10
    assert config_version == 3

    with pytest.raises(AppException, match="视频配置版本冲突"):
        await _storyboard_video_parameters(
            None,
            storyboard,
            model,
            "720p",
            use_saved_config=False,
            model_cache={model.id: model},
            requested_duration_seconds=10,
            expected_config_version=2,
        )


def test_multimodal_prompt_uses_reference_tokens_without_persisting_layout() -> None:
    storyboard = SimpleNamespace(
        video_prompt=None,
        extra={
            "agent_storyboard_prompt_template": (
                "画面风格：写实漫剧风格\n"
                "视频中不得出现任何字幕、文字叠加，保持纯画面。不要BGM，不要配乐。\n"
                "镜头1：画面内容：{{asset:character_1}}进入{{asset:scene_1}}"
            )
        },
    )
    manifest = [
        {
            "reference_token": "@图片1",
            "asset_type": "character",
            "label": "沈砚夜行变装",
        },
        {
            "reference_token": "@图片2",
            "asset_type": "scene",
            "label": "深夜旧宅",
        },
    ]
    prompt = _compile_prompt(
        storyboard,
        {"character_1": "沈砚夜行变装", "scene_1": "深夜旧宅"},
        {"character_1": "@图片1", "scene_1": "@图片2"},
        manifest,
        "镜头推进稍慢",
    )
    assert "沈砚夜行变装（参考@图片1）" in prompt
    assert "深夜旧宅（参考@图片2）" in prompt
    assert "@图片1 作为人物" in prompt
    assert "@图片2 作为场景" in prompt
    assert "不得复刻白底、拼版或三视图布局" in prompt
    assert "用户补充要求只能调整" in prompt


@pytest.mark.asyncio
async def test_multimodal_input_rejects_selected_variant_without_image(monkeypatch) -> None:
    asset_id = uuid4()
    variant_id = uuid4()
    production = SimpleNamespace(id=uuid4(), project_id=uuid4(), user_id=uuid4())
    storyboard = SimpleNamespace(
        video_prompt=None,
        extra={
            "agent_asset_bindings": [
                {
                    "binding_key": "character_1",
                    "asset_type": "character",
                    "asset_id": str(asset_id),
                    "variant_id": str(variant_id),
                }
            ],
            "agent_asset_binding_labels": {"character_1": "沈砚夜行变装"},
            "agent_storyboard_prompt_template": "画面内容：{{asset:character_1}}",
        },
    )

    async def load_assets(*_args):
        return {
            "character": {
                asset_id: SimpleNamespace(name="沈砚", reference_image="base.png")
            },
            "scene": {},
            "prop": {},
        }

    async def load_variants(*_args):
        return {
            variant_id: SimpleNamespace(
                canonical_name="沈砚夜行变装",
                asset_type="character",
                reference_image=None,
            )
        }

    monkeypatch.setattr(
        "app.services.agent.storyboard_video_inputs._load_assets", load_assets
    )
    monkeypatch.setattr(
        "app.services.agent.storyboard_video_inputs._load_variants", load_variants
    )
    with pytest.raises(AppException, match="缺少参考图") as exc_info:
        await build_agent_storyboard_video_input(
            None,
            production,
            storyboard,
            max_images=9,
        )
    assert exc_info.value.code == 40994
    assert exc_info.value.data["missing_references"][0]["variant_id"] == str(
        variant_id
    )


@pytest.mark.asyncio
async def test_multimodal_input_uses_only_mentioned_assets_in_mention_order(
    monkeypatch,
) -> None:
    character_id = uuid4()
    scene_id = uuid4()
    prop_id = uuid4()
    production = SimpleNamespace(id=uuid4(), project_id=uuid4(), user_id=uuid4())
    storyboard = SimpleNamespace(
        video_prompt=None,
        extra={
            "agent_asset_bindings": [
                {
                    "binding_key": "character_1",
                    "asset_type": "character",
                    "asset_id": str(character_id),
                },
                {
                    "binding_key": "scene_1",
                    "asset_type": "scene",
                    "asset_id": str(scene_id),
                },
                {
                    "binding_key": "prop_1",
                    "asset_type": "prop",
                    "asset_id": str(prop_id),
                },
            ],
            "agent_asset_binding_labels": {
                "character_1": "沈砚",
                "scene_1": "旧宅",
                "prop_1": "玉佩",
            },
            "agent_storyboard_prompt_template": (
                "{{asset:scene_1}}内，{{asset:character_1}}缓慢走近镜头，"
                "{{asset:character_1}}停下观察。"
            ),
        },
    )

    async def load_assets(*_args):
        return {
            "character": {
                character_id: SimpleNamespace(name="沈砚", reference_image="character.png")
            },
            "scene": {scene_id: SimpleNamespace(name="旧宅", reference_image="scene.png")},
            "prop": {prop_id: SimpleNamespace(name="玉佩", reference_image="prop.png")},
        }

    async def load_variants(*_args):
        return {}

    async def resolve_urls(_db, urls):
        return [f"resolved-{url}" for url in urls]

    monkeypatch.setattr(
        "app.services.agent.storyboard_video_inputs._load_assets", load_assets
    )
    monkeypatch.setattr(
        "app.services.agent.storyboard_video_inputs._load_variants", load_variants
    )
    monkeypatch.setattr(
        "app.services.agent.storyboard_video_inputs.resolve_storyboard_reference_image_urls",
        resolve_urls,
    )

    result = await build_agent_storyboard_video_input(
        None,
        production,
        storyboard,
        max_images=2,
    )

    assert result.reference_images == ["resolved-scene.png", "resolved-character.png"]
    assert [item["binding_key"] for item in result.reference_manifest] == [
        "scene_1",
        "character_1",
    ]
    assert "prop.png" not in result.reference_images
    assert "旧宅（参考@图片1）" in result.prompt
    assert "沈砚（参考@图片2）" in result.prompt

    storyboard.extra["agent_storyboard_prompt_template"] = "人物进入场景并拿起道具"
    legacy_result = await build_agent_storyboard_video_input(
        None,
        production,
        storyboard,
        max_images=3,
    )
    assert legacy_result.reference_images == [
        "resolved-character.png",
        "resolved-scene.png",
        "resolved-prop.png",
    ]


def test_invalidated_video_is_not_reported_as_current_success() -> None:
    task_id = uuid4()
    storyboard = SimpleNamespace(
        id=uuid4(),
        shot_number=1,
        title="第一分镜组",
        duration_suggestion="6秒",
        extra={
            "agent_storyboard_revision": 2,
            "estimated_duration_seconds": 6,
            "video_generation_status": "invalidated",
            "video_generation_result": "https://example.com/old.mp4",
            "video_generation_task_record_id": str(task_id),
        },
    )
    task = SimpleNamespace(
        id=task_id,
        status="success",
        ai_model_id=uuid4(),
        generation_type="storyboard_video",
        extra={"resolution": "720p"},
    )
    result = _video_item(storyboard, {task_id: task})
    assert result["video"]["status"] == "invalidated"
