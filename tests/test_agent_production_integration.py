import os
import re
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401
from app.api.deps import get_current_user
from app.core.config import settings
from app.core.timezone import beijing_datetime
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.agent_core_asset import AgentCoreAssetLock
from app.models.agent_production import (
    AgentCheckpoint,
    AgentControllerState,
    AgentEvent,
    AgentProduction,
    AgentStep,
    ProjectSourceDocument,
)
from app.models.agent_story_bible import (
    AgentAssetCandidate,
    AgentAssetVariant,
    SeriesBibleVersion,
)
from app.models.agent_storyboard_media import AgentStoryboardMediaRequest
from app.models.agent_workflow import AgentWorkflowStepState
from app.models.ai_model import AiModel
from app.models.project import Project
from app.models.project_asset import ProjectCharacter, ProjectProp, ProjectScene
from app.models.project_chapter import ProjectChapter
from app.models.project_generated_asset import ProjectGeneratedAsset
from app.models.project_storyboard import ProjectStoryboard
from app.models.style import Style
from app.models.task_record import UserTaskRecord
from app.models.user import User
from app.schemas.upload import UploadFileOut
from app.services.agent_production_controller import (
    advance_agent_batch_production,
    fail_agent_controller_claim,
    finish_agent_controller_claim,
    list_agent_controller_candidates,
    queue_agent_controller_claim,
    start_agent_controller_claim,
)
from app.services.agent_reviews import claim_agent_delivery, run_agent_delivery
from app.services.agent_source_analysis import (
    _complete_source_analysis,
    _merge_incremental_episode_plan,
    advance_source_analysis,
    build_agent_text_prompt,
)
from app.services.agent_story_bibles import initialize_script_assets
from app.services.agent_storyboard_bindings import bind_storyboards_to_core_lock
from app.services.model_runner import ModelRunResult
from app.services.project_asset_generation import get_project_with_style_or_404
from app.services.project_asset_generation import run_asset_image_generation_in_worker
from app.services.project_generated_assets import record_storyboard_video_generation_success
from app.services.project_storyboards import run_storyboard_analysis_in_worker


@pytest.mark.asyncio
async def test_episode_video_contract_uses_global_variant_assets_and_is_idempotent(
    agent_api,
    monkeypatch,
) -> None:
    story_step = AgentStep(
        id=uuid4(),
        production_id=agent_api.production.id,
        stage="story_bible",
        scope_type="production",
        scope_id=agent_api.production.id,
        status="completed",
        input_version=1,
        output_version=1,
        progress_current=1,
        progress_total=1,
        attempt_count=1,
        extra={},
    )
    core_step = AgentStep(
        id=uuid4(),
        production_id=agent_api.production.id,
        stage="core_assets",
        scope_type="production",
        scope_id=agent_api.production.id,
        status="completed",
        input_version=1,
        output_version=1,
        progress_current=1,
        progress_total=1,
        attempt_count=1,
        extra={},
    )
    bible = SeriesBibleVersion(
        id=uuid4(),
        project_id=agent_api.project.id,
        production_id=agent_api.production.id,
        step_id=story_step.id,
        version=1,
        status="confirmed",
        content={},
        created_by=agent_api.owner.id,
        confirmed_by=agent_api.owner.id,
    )
    character = ProjectCharacter(
        id=uuid4(),
        project_id=agent_api.project.id,
        user_id=agent_api.owner.id,
        name="沈砚",
        aliases=[],
        reference_image="https://example.com/shenyan-base.png",
        extra={},
        is_enabled=True,
    )
    candidate = AgentAssetCandidate(
        id=uuid4(),
        project_id=agent_api.project.id,
        production_id=agent_api.production.id,
        bible_version_id=bible.id,
        user_id=agent_api.owner.id,
        asset_type="character",
        candidate_key="9" * 64,
        canonical_name="沈砚",
        aliases=[],
        source_chapter_ids=[str(agent_api.chapter.id)],
        confidence=1,
        merge_reason="同一人物",
        review_status="materialized",
        content={},
        materialized_asset_id=character.id,
        lock_version=1,
    )
    variant = AgentAssetVariant(
        id=uuid4(),
        project_id=agent_api.project.id,
        production_id=agent_api.production.id,
        bible_version_id=bible.id,
        base_candidate_id=candidate.id,
        user_id=agent_api.owner.id,
        asset_type="character",
        variant_key="8" * 64,
        canonical_name="沈砚夜行装",
        variant_type="costume",
        description="黑色夜行服",
        trigger_reason="夜探旧宅",
        episode_numbers=[1],
        source_evidence=[],
        confidence=1,
        review_status="ready",
        content={},
        reference_image="https://example.com/shenyan-night.png",
        extra={},
        lock_version=1,
    )
    core_lock = AgentCoreAssetLock(
        id=uuid4(),
        project_id=agent_api.project.id,
        production_id=agent_api.production.id,
        bible_version_id=bible.id,
        step_id=core_step.id,
        version=1,
        status="active",
        assets=[
            {
                "asset_type": "character",
                "asset_id": str(character.id),
                "candidate_id": str(candidate.id),
                "name": character.name,
                "reference_image": character.reference_image,
            }
        ],
        impact={},
        idempotency_key="episode-media-core-lock-v1",
        created_by=agent_api.owner.id,
    )
    storyboard = ProjectStoryboard(
        id=uuid4(),
        project_id=agent_api.project.id,
        chapter_id=agent_api.chapter.id,
        user_id=agent_api.owner.id,
        shot_number=1,
        title="发现玉佩",
        source_content="沈砚在旧宅发现玉佩。",
        action="沈砚弯腰拾起玉佩",
        video_prompt="画面风格：写实漫剧风格\n镜头1：沈砚拾起玉佩",
        duration_suggestion="6秒",
        characters=["沈砚夜行装"],
        props=[],
        extra={
            "agent_storyboard_revision": 1,
            "agent_storyboard_status": "ready",
            "agent_storyboard_validation_errors": [],
            "agent_asset_ids": {
                "character": [str(character.id)],
                "scene": [],
                "prop": [],
            },
            "agent_asset_variant_ids": {
                "character": {str(character.id): str(variant.id)},
                "scene": {},
                "prop": {},
            },
            "agent_asset_bindings": [
                {
                    "binding_key": "character_1",
                    "asset_type": "character",
                    "asset_id": str(character.id),
                    "variant_id": str(variant.id),
                }
            ],
            "agent_asset_binding_labels": {"character_1": "沈砚夜行装"},
            "agent_storyboard_prompt_template": (
                "画面风格：写实漫剧风格\n"
                "视频中不得出现任何字幕、文字叠加，保持纯画面。不要BGM，不要配乐。\n"
                "镜头1：{{asset:character_1}}拾起玉佩"
            ),
            "agent_storyboard_prompt": (
                "画面风格：写实漫剧风格\n"
                "视频中不得出现任何字幕、文字叠加，保持纯画面。不要BGM，不要配乐。\n"
                "镜头1：沈砚夜行装拾起玉佩"
            ),
            "agent_unbound_asset_names": {
                "character": [],
                "scene": [],
                "prop": [],
            },
            "estimated_duration_seconds": 6,
        },
        is_enabled=True,
    )
    agent_api.project.project_kind = "agent"
    agent_api.production.current_stage = "batch_production"
    agent_api.production.production_spec = {
        **(agent_api.production.production_spec or {}),
        "workflow_version": 2,
        "pilot_episode_count": 0,
    }
    agent_api.chapter.extra = {
        **(agent_api.chapter.extra or {}),
        "storyboard_analysis_status": "success",
        "agent_storyboard_episode_revision": 1,
    }
    agent_api.owner.points_balance = 1000
    agent_api.session.add_all([story_step, core_step, character, storyboard])
    await agent_api.session.flush()
    agent_api.session.add(bible)
    await agent_api.session.flush()
    agent_api.session.add(candidate)
    await agent_api.session.flush()
    agent_api.session.add_all([variant, core_lock])
    await agent_api.session.commit()

    monkeypatch.setattr(
        "app.tasks.project_storyboard_video.run_project_storyboard_video_generation.apply_async",
        lambda **_kwargs: None,
    )
    prefix = (
        f"/api/v1/agent-productions/{agent_api.production.id}"
        f"/episodes/{agent_api.chapter.id}"
    )
    asset_options = await agent_api.client.get(
        f"{prefix}/storyboards/{storyboard.id}/asset-options"
    )
    assert asset_options.status_code == 200
    character_option = asset_options.json()["data"]["items"][0]
    assert character_option["bound"] is True
    assert character_option["selected_variant_id"] == str(variant.id)
    assert character_option["variants"][0]["selected"] is True
    assert character_option["mention_text"] == "@沈砚夜行装"
    assert character_option["mention_reference_image"] == variant.reference_image
    assert character_option["mention_enabled"] is True
    storyboard_package = await agent_api.client.get(
        f"/api/v1/agent-productions/{agent_api.production.id}/storyboards"
    )
    assert storyboard_package.status_code == 200
    storyboard_data = storyboard_package.json()["data"]["episodes"][0]["storyboards"][0]
    assert "{{asset:character_1}}" in storyboard_data["prompt_template"]
    invalid_mention = await agent_api.client.patch(
        f"/api/v1/agent-productions/{agent_api.production.id}/storyboards/{storyboard.id}",
        json={
            "expected_core_asset_lock_version": 1,
            "expected_revision": 1,
            "storyboard_prompt": (
                "画面风格：写实漫剧风格\n"
                "视频中不得出现任何字幕、文字叠加，保持纯画面。"
                "不要BGM，不要配乐。\n"
                "镜头1：{{asset:prop_9}}出现在桌面上"
            ),
        },
    )
    assert invalid_mention.status_code == 409
    assert invalid_mention.json()["code"] == 40962
    assert invalid_mention.json()["data"]["invalid_binding_keys"] == ["prop_9"]

    configured = await agent_api.client.put(
        f"{prefix}/storyboards/{storyboard.id}/video-config",
        json={
            "expected_core_asset_lock_version": 1,
            "expected_storyboard_revision": 1,
            "expected_config_version": 0,
            "video_model_id": str(agent_api.video_model.id),
            "video_resolution": "1080p",
            "estimated_duration_seconds": 6,
        },
    )
    assert configured.status_code == 200
    assert configured.json()["data"]["config_version"] == 1
    initial = await agent_api.client.get(f"{prefix}/videos")
    assert initial.status_code == 200
    assert initial.json()["data"]["can_generate"] is True
    video_payload = {
        "expected_core_asset_lock_version": 1,
        "expected_episode_revision": 1,
        "idempotency_key": "episode-video-generation-v1",
        "video_model_id": str(agent_api.video_model.id),
        "video_resolution": "1080p",
    }
    video_response = await agent_api.client.post(
        f"{prefix}/video-generations", json=video_payload
    )
    assert video_response.status_code == 200
    video_data = video_response.json()["data"]
    assert video_data["submitted_count"] == 1
    video_task = await agent_api.session.get(
        UserTaskRecord,
        UUID(video_data["items"][0]["task_record_id"]),
    )
    assert video_task.ai_model_id == agent_api.video_model.id
    assert video_task.extra["resolution"] == "1080p"
    assert video_task.extra["model_extra"]["duration_seconds"] == 6
    assert video_task.extra["agent_stage"] == "episode_videos"
    assert video_task.extra["reference_images"] == [
        "https://example.com/shenyan-night.png"
    ]
    assert video_task.extra["agent_reference_manifest"][0]["reference_token"] == "@图片1"
    assert video_task.extra["agent_reference_manifest"][0]["variant_id"] == str(variant.id)
    assert "@图片1" in video_task.prompt
    assert "不得复刻白底、拼版或三视图布局" in video_task.prompt
    assert "https://example.com/shenyan-base.png" not in video_task.extra["reference_images"]

    replay = await agent_api.client.post(
        f"{prefix}/video-generations", json=video_payload
    )
    assert replay.status_code == 200
    assert replay.json()["data"]["idempotent_replay"] is True
    assert replay.json()["data"]["items"][0]["task_record_id"] == str(video_task.id)
    requests = (
        await agent_api.session.execute(select(AgentStoryboardMediaRequest))
    ).scalars().all()
    assert len(requests) == 1

    first_history = await record_storyboard_video_generation_success(
        agent_api.session,
        task_record=video_task,
        storyboard=storyboard,
        content="https://example.com/episode-1-shot-1.mp4",
        result_extra={},
        last_frame_url=None,
    )
    await agent_api.session.commit()
    storyboard_video_payload = {
        **video_payload,
        "expected_storyboard_revision": 1,
        "expected_video_config_version": 1,
        "duration_seconds": 6,
        "idempotency_key": "storyboard-video-generation-v1",
        "prompt": "加强沈砚拾起玉佩前的迟疑和眼神变化",
    }
    storyboard_video_response = await agent_api.client.post(
        f"{prefix}/storyboards/{storyboard.id}/video-generations",
        json=storyboard_video_payload,
    )
    assert storyboard_video_response.status_code == 200
    storyboard_video_data = storyboard_video_response.json()["data"]
    assert storyboard_video_data["submitted_count"] == 1
    regenerated_task = await agent_api.session.get(
        UserTaskRecord,
        UUID(storyboard_video_data["items"][0]["task_record_id"]),
    )
    assert regenerated_task.id != video_task.id
    assert "加强沈砚拾起玉佩前的迟疑和眼神变化" in regenerated_task.prompt
    assert regenerated_task.extra["reference_images"] == [
        "https://example.com/shenyan-night.png"
    ]
    second_history = await record_storyboard_video_generation_success(
        agent_api.session,
        task_record=regenerated_task,
        storyboard=storyboard,
        content="https://example.com/episode-1-shot-1-v2.mp4",
        result_extra={},
        last_frame_url=None,
    )
    await agent_api.session.commit()
    versions = await agent_api.client.get(
        f"{prefix}/storyboards/{storyboard.id}/video-versions"
    )
    assert versions.status_code == 200
    versions_data = versions.json()["data"]
    assert versions_data["total"] == 2
    assert versions_data["selection_required"] is True
    assert versions_data["selected_history_id"] == str(first_history.id)
    assert versions_data["items"][0]["history_id"] == str(second_history.id)
    assert versions_data["items"][0]["asset_bindings"][0]["variant_id"] == str(
        variant.id
    )
    assert versions_data["items"][0]["requested_duration_seconds"] == 6
    assert versions_data["items"][0]["reference_manifest"][0][
        "reference_token"
    ] == "@图片1"
    assert versions_data["items"][0]["provider_parameters"][
        "duration_seconds"
    ] == 6
    waiting_selection = await agent_api.client.get(f"{prefix}/videos")
    assert waiting_selection.status_code == 200
    assert waiting_selection.json()["data"]["status"] == "selection_required"
    assert waiting_selection.json()["data"]["can_generate"] is False
    selected = await agent_api.client.put(
        f"{prefix}/storyboards/{storyboard.id}/primary-video",
        json={
            "expected_selection_revision": versions_data["selection_revision"],
            "history_id": str(second_history.id),
        },
    )
    assert selected.status_code == 200
    assert selected.json()["data"]["history_id"] == str(second_history.id)
    assert selected.json()["data"]["selection_required"] is False
    requests = (
        await agent_api.session.execute(select(AgentStoryboardMediaRequest))
    ).scalars().all()
    assert len(requests) == 2

    storyboard.extra = {
        **(storyboard.extra or {}),
        "agent_storyboard_revision": 2,
    }
    agent_api.chapter.extra = {
        **(agent_api.chapter.extra or {}),
        "agent_storyboard_episode_revision": 2,
    }
    await agent_api.session.commit()
    replay_after_edit = await agent_api.client.post(
        f"{prefix}/video-generations",
        json=video_payload,
    )
    assert replay_after_edit.status_code == 200
    assert replay_after_edit.json()["data"]["idempotent_replay"] is True

    stale = await agent_api.client.post(
        f"{prefix}/video-generations",
        json={
            **video_payload,
            "idempotency_key": "episode-video-generation-v2",
        },
    )
    assert stale.status_code == 409


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_DB_INTEGRATION_TESTS") != "1",
        reason="set RUN_DB_INTEGRATION_TESTS=1 to run PostgreSQL integration tests",
    ),
]


@pytest.fixture
async def agent_api():
    schema_name = f"agent_f_{uuid4().hex}"
    assert re.fullmatch(r"agent_f_[0-9a-f]{32}", schema_name)
    admin_engine = create_async_engine(settings.database_url, poolclass=NullPool)
    test_engine = create_async_engine(
        settings.database_url,
        poolclass=NullPool,
        execution_options={"schema_translate_map": {None: schema_name}},
    )
    async with admin_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
    try:
        async with test_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(
            test_engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        async with session_factory() as session:
            owner = User(
                id=uuid4(),
                account=f"owner-{uuid4().hex}",
                password_hash="test",
                nickname="Owner",
                is_enabled=True,
                points_balance=100,
            )
            outsider = User(
                id=uuid4(),
                account=f"outsider-{uuid4().hex}",
                password_hash="test",
                nickname="Outsider",
                is_enabled=True,
                points_balance=100,
            )
            style = Style(
                id=uuid4(),
                name=f"integration-{uuid4().hex}",
                cover="https://example.com/style.png",
                prompt="写实漫剧风格",
                is_enabled=True,
            )
            project = Project(
                id=uuid4(),
                user_id=owner.id,
                style_id=style.id,
                name="Epic F 集成测试项目",
                cover="https://example.com/cover.png",
                description="integration",
                generation_ratio="16:9",
                project_kind="standard",
                is_enabled=True,
            )
            text_model = AiModel(
                id=uuid4(),
                nickname="Epic F 文本模型",
                model_id="gpt-5.5",
                vendor="integration",
                model_type="text",
                points_cost=7,
                is_enabled=True,
                is_agent_default=True,
                capabilities={},
            )
            image_model = AiModel(
                id=uuid4(),
                nickname="Epic G 图像模型",
                model_id="gpt-image-2",
                vendor="integration",
                model_type="image",
                points_cost=3,
                is_enabled=True,
                is_agent_default=True,
                capabilities={},
            )
            video_model = AiModel(
                id=uuid4(),
                nickname="Epic H 视频模型",
                model_id=f"integration-video-{uuid4().hex}",
                vendor="integration",
                model_type="video",
                points_cost=2,
                is_enabled=True,
                is_agent_default=True,
                capabilities={},
            )
            source = ProjectSourceDocument(
                id=uuid4(),
                project_id=project.id,
                user_id=owner.id,
                source_type="text",
                content="沈砚在旧宅发现玉佩。黑衣人随后现身。",
                content_hash="a" * 64,
                character_count=20,
                version=1,
                parse_status="success",
                extra={},
            )
            production = AgentProduction(
                id=uuid4(),
                project_id=project.id,
                user_id=owner.id,
                source_document_id=source.id,
                status="planning",
                current_stage="story_bible",
                mode="supervised",
                production_spec={
                    "text_model_id": str(text_model.id),
                    "image_model_id": str(image_model.id),
                    "video_model_id": str(video_model.id),
                },
                estimated_points=0,
                consumed_points=0,
                lock_version=1,
                extra={},
            )
            source_step = AgentStep(
                id=uuid4(),
                production_id=production.id,
                stage="source_analysis",
                scope_type="production",
                scope_id=production.id,
                status="completed",
                input_version=1,
                output_version=1,
                progress_current=3,
                progress_total=3,
                attempt_count=1,
                extra={
                    "global_analysis": {
                        "story_summary": "旧宅寻物",
                        "worldview": "现代悬疑",
                        "characters": [
                            {"name": "沈砚", "aliases": ["阿砚"]},
                            {"name": "黑衣人", "aliases": ["影子"]},
                            {"name": "刺客", "aliases": ["影子"]},
                        ],
                        "scenes": [{"name": "旧宅", "atmosphere": "阴冷"}],
                        "props": [{"name": "玉佩", "function": "信物"}],
                    },
                    "episode_plan": {
                        "episodes": [
                            {
                                "episode_number": 1,
                                "characters": ["沈砚", "黑衣人"],
                                "scenes": ["旧宅"],
                                "props": ["玉佩"],
                            }
                        ]
                    },
                },
            )
            chapter = ProjectChapter(
                id=uuid4(),
                project_id=project.id,
                user_id=owner.id,
                title="第一集",
                content=source.content,
                processed_content=source.content,
                process_status="completed",
                sort_order=1,
                extra={
                    "agent_production_id": str(production.id),
                    "episode_number": 1,
                },
                is_enabled=True,
            )
            session.add_all(
                [
                    owner,
                    outsider,
                    style,
                    project,
                    text_model,
                    image_model,
                    video_model,
                    source,
                    production,
                    source_step,
                    chapter,
                ]
            )
            await session.commit()

            identity = {"user": owner}

            async def override_db():
                yield session

            async def override_current_user():
                return identity["user"]

            app.dependency_overrides[get_db] = override_db
            app.dependency_overrides[get_current_user] = override_current_user
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                yield SimpleNamespace(
                    client=client,
                    identity=identity,
                    owner=owner,
                    outsider=outsider,
                    style=style,
                    project=project,
                    production=production,
                    session=session,
                    text_model=text_model,
                    image_model=image_model,
                    video_model=video_model,
                    chapter=chapter,
                )
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)
        await test_engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema_name}" CASCADE'))
        await admin_engine.dispose()


@pytest.mark.asyncio
async def test_asset_generation_project_lookup_accepts_internal_agent_project(
    agent_api,
) -> None:
    agent_project = Project(
        id=uuid4(),
        user_id=agent_api.owner.id,
        style_id=agent_api.style.id,
        name="Agent 资产生图项目",
        cover="https://example.com/agent-cover.png",
        description="asset generation lookup regression",
        generation_ratio="16:9",
        project_kind="agent",
        is_enabled=True,
    )
    agent_api.session.add(agent_project)
    await agent_api.session.commit()

    loaded = await get_project_with_style_or_404(
        agent_api.session,
        agent_project.id,
        agent_api.owner.id,
    )

    assert loaded.id == agent_project.id
    assert loaded.style.id == agent_api.style.id


@pytest.mark.asyncio
async def test_four_step_workflow_records_episode_state_and_blocks_future_step(
    agent_api,
) -> None:
    prefix = f"/api/v1/agent-productions/{agent_api.production.id}"

    workflow = await agent_api.client.get(f"{prefix}/workflow")

    assert workflow.status_code == 200
    data = workflow.json()["data"]
    assert data["mode"] == "supervised"
    assert data["current_step"] == 1
    assert [item["step_code"] for item in data["steps"]] == [
        "script_processing",
        "asset_confirmation",
        "storyboard_generation",
        "video_editing",
    ]
    assert [item["status"] for item in data["steps"]] == [
        "processing",
        "not_started",
        "not_started",
        "not_started",
    ]
    assert len(data["episodes"]) == 1
    assert len(data["episodes"][0]["steps"]) == 4

    records = await agent_api.session.execute(
        select(AgentWorkflowStepState).where(
            AgentWorkflowStepState.production_id == agent_api.production.id
        )
    )
    assert len(records.scalars().all()) == 8

    blocked = await agent_api.client.get(f"{prefix}/core-assets")
    assert blocked.status_code == 409
    assert blocked.json()["code"] == 40990
    assert blocked.json()["data"] == {
        "requested_step": 2,
        "required_step": 1,
        "current_step": 1,
        "required_status": "completed",
    }

    agent_api.production.current_stage = "core_assets"
    await agent_api.session.commit()
    advanced = await agent_api.client.get(f"{prefix}/workflow")
    advanced_data = advanced.json()["data"]
    assert advanced_data["current_step"] == 2
    assert [item["status"] for item in advanced_data["steps"][:2]] == [
        "completed",
        "waiting_review",
    ]


@pytest.mark.asyncio
async def test_supervised_script_supplement_creates_incremental_asset_version(
    agent_api,
    monkeypatch,
) -> None:
    source_step = (
        await agent_api.session.execute(
            select(AgentStep).where(
                AgentStep.production_id == agent_api.production.id,
                AgentStep.stage == "source_analysis",
            )
        )
    ).scalar_one()
    source_step.status = "waiting_approval"
    source_step.output_version = 1
    checkpoint = AgentCheckpoint(
        id=uuid4(),
        production_id=agent_api.production.id,
        step_id=source_step.id,
        checkpoint_type="script_review",
        status="pending",
        summary="等待审核",
        impact={},
        extra={},
    )
    bible = SeriesBibleVersion(
        id=uuid4(),
        project_id=agent_api.project.id,
        production_id=agent_api.production.id,
        step_id=source_step.id,
        version=1,
        status="draft",
        content={
            "characters": [{"name": "沈砚", "aliases": ["阿砚"]}],
            "character_variants": [],
            "scenes": [{"name": "旧宅"}],
            "scene_variants": [],
            "props": [],
            "prop_variants": [],
        },
        created_by=agent_api.owner.id,
    )
    agent_api.production.status = "waiting_approval"
    agent_api.production.current_stage = "script_review"
    agent_api.production.mode = "supervised"
    agent_api.production.production_spec = {
        **(agent_api.production.production_spec or {}),
        "workflow_version": 2,
    }
    agent_api.session.add(checkpoint)
    await agent_api.session.flush()
    agent_api.session.add(bible)
    await agent_api.session.commit()

    queued = []

    async def capture_enqueue(_db, production_id, step_id):
        queued.append((production_id, step_id))

    monkeypatch.setattr(
        "app.services.agent_script_supplements._enqueue_source_analysis",
        capture_enqueue,
    )
    response = await agent_api.client.post(
        f"/api/v1/agent-productions/{agent_api.production.id}/script-supplements/from-text",
        json={"content": "十年后，沈先生带着怀表重返旧宅。"},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["source_version"] == 2
    assert data["status"] == "planning"
    assert data["current_stage"] == "source_analysis"
    assert len(queued) == 1
    await agent_api.session.refresh(source_step)
    await agent_api.session.refresh(checkpoint)
    assert source_step.status == "invalidated"
    assert checkpoint.status == "rejected"
    new_source = await agent_api.session.get(
        ProjectSourceDocument,
        UUID(data["source_document_id"]),
    )
    assert new_source.content.endswith("十年后，沈先生带着怀表重返旧宅。")
    new_step = await agent_api.session.get(AgentStep, UUID(data["step_id"]))
    assert new_step.extra["incremental"] is True
    assert new_step.extra["analysis_scope"] == "full"
    assert new_step.output_version == 1

    new_bible, candidates, variants, _warnings = await initialize_script_assets(
        agent_api.session,
        agent_api.production,
        new_step,
        {
            "characters": [
                {"name": "沈先生", "aliases": ["沈砚"], "appearance": "鬓角斑白"}
            ],
            "character_variants": [
                {
                    "base_name": "沈砚",
                    "name": "沈砚中年造型",
                    "variant_type": "age",
                    "description": "鬓角斑白",
                    "trigger_reason": "十年后",
                    "source_start": len(new_source.content) - 10,
                    "source_end": len(new_source.content),
                }
            ],
            "scenes": [{"name": "旧宅"}],
            "scene_variants": [],
            "props": [{"name": "怀表"}],
            "prop_variants": [],
        },
        {"episodes": []},
    )
    await agent_api.session.commit()

    assert new_bible.version == 2
    assert bible.status == "superseded"
    assert len([item for item in candidates if item.asset_type == "character"]) == 1
    merged_character = next(item for item in candidates if item.asset_type == "character")
    assert {"阿砚", "沈先生"} <= set(merged_character.aliases)
    assert any(item.canonical_name == "怀表" for item in candidates)
    assert variants[0].base_candidate_id == merged_character.id


@pytest.mark.asyncio
async def test_confirmed_script_supplement_keeps_completed_steps_accessible(
    agent_api,
    monkeypatch,
) -> None:
    source_step = (
        await agent_api.session.execute(
            select(AgentStep).where(
                AgentStep.production_id == agent_api.production.id,
                AgentStep.stage == "source_analysis",
            )
        )
    ).scalar_one()
    source_step.status = "completed"
    checkpoint = AgentCheckpoint(
        id=uuid4(),
        production_id=agent_api.production.id,
        step_id=source_step.id,
        checkpoint_type="script_review",
        status="approved",
        summary="已确认",
        impact={},
        extra={},
    )
    bible = SeriesBibleVersion(
        id=uuid4(),
        project_id=agent_api.project.id,
        production_id=agent_api.production.id,
        step_id=source_step.id,
        version=1,
        status="confirmed",
        content={},
        created_by=agent_api.owner.id,
        confirmed_by=agent_api.owner.id,
    )
    core_lock = AgentCoreAssetLock(
        id=uuid4(),
        project_id=agent_api.project.id,
        production_id=agent_api.production.id,
        bible_version_id=bible.id,
        step_id=source_step.id,
        version=1,
        status="active",
        assets=[],
        impact={},
        idempotency_key="supplement-core-lock-v1",
        created_by=agent_api.owner.id,
    )
    agent_api.production.mode = "supervised"
    agent_api.production.status = "running"
    agent_api.production.current_stage = "batch_production"
    agent_api.production.production_spec = {
        **(agent_api.production.production_spec or {}),
        "workflow_version": 2,
    }
    agent_api.session.add(checkpoint)
    await agent_api.session.flush()
    agent_api.session.add(bible)
    await agent_api.session.flush()
    agent_api.session.add(core_lock)
    previous_source = await agent_api.session.get(
        ProjectSourceDocument,
        agent_api.production.source_document_id,
    )
    previous_plan_id = uuid4()
    source_step.extra = {
        **(source_step.extra or {}),
        "episode_plan": {
            "planning_summary": "原剧本分为一集",
            "episodes": [
                {
                    "plan_id": str(previous_plan_id),
                    "episode_number": 1,
                    "title": "第一集",
                    "content": previous_source.content,
                    "opening_hook": "沈砚进入旧宅",
                    "goal": "调查旧宅",
                    "conflict": "线索不足",
                    "climax": "发现玉佩",
                    "ending_hook": "玉佩来历成谜",
                    "source_start": 0,
                    "source_end": len(previous_source.content),
                }
            ],
        },
    }
    agent_api.chapter.extra = {
        **(agent_api.chapter.extra or {}),
        "episode_plan_id": str(previous_plan_id),
        "source_start": 0,
        "source_end": len(previous_source.content),
        "storyboard_analysis_status": "success",
    }
    await agent_api.session.commit()

    queued = []

    async def capture_enqueue(_db, production_id, step_id):
        queued.append((production_id, step_id))

    monkeypatch.setattr(
        "app.services.agent_script_supplements._enqueue_source_analysis",
        capture_enqueue,
    )
    prefix = f"/api/v1/agent-productions/{agent_api.production.id}"
    response = await agent_api.client.post(
        f"{prefix}/script-supplements/from-text",
        json={"content": "次日，沈砚再次回到旧宅。"},
    )

    assert response.status_code == 200
    assert len(queued) == 1
    await agent_api.session.refresh(source_step)
    await agent_api.session.refresh(checkpoint)
    await agent_api.session.refresh(bible)
    assert source_step.status == "completed"
    assert checkpoint.status == "approved"
    assert bible.status == "confirmed"

    new_step = await agent_api.session.get(AgentStep, UUID(response.json()["data"]["step_id"]))
    new_source = await agent_api.session.get(
        ProjectSourceDocument,
        agent_api.production.source_document_id,
    )
    assert new_step.extra["analysis_scope"] == "incremental"
    incremental_start = new_step.extra["incremental_source_start"]
    incremental_end = new_step.extra["incremental_source_end"]
    assert (
        new_source.content[incremental_start:incremental_end]
        == "\n\n次日，沈砚再次回到旧宅。"
    )
    incremental_episode_plan = {
        "episodes": [
            {
                "title": "第二集",
                "content": "次日，沈砚再次回到旧宅。",
                "opening_hook": "沈砚次日返回",
                "goal": "继续调查",
                "conflict": "旧宅线索中断",
                "climax": "沈砚重新进入旧宅",
                "ending_hook": "旧宅出现新变化",
                "source_start": incremental_start,
                "source_end": len(new_source.content),
            }
        ]
    }
    episode_plan = await _merge_incremental_episode_plan(
        agent_api.session,
        agent_api.production,
        new_step,
        incremental_episode_plan,
    )
    new_bible, _candidates, _variants, warnings = await initialize_script_assets(
        agent_api.session,
        agent_api.production,
        new_step,
        {
            "characters": [{"name": "沈砚"}],
            "character_variants": [],
            "scenes": [{"name": "旧宅"}],
            "scene_variants": [],
            "props": [],
            "prop_variants": [],
        },
        episode_plan,
    )
    new_step.status = "waiting_approval"
    new_step.extra = {
        **(new_step.extra or {}),
        "episode_plan": episode_plan,
        "script_warnings": warnings,
        "bible_version_id": str(new_bible.id),
    }
    agent_api.session.add(
        AgentCheckpoint(
            id=uuid4(),
            production_id=agent_api.production.id,
            step_id=new_step.id,
            checkpoint_type="script_review",
            status="pending",
            summary="补充剧本处理完成",
            impact={},
            extra={"bible_version_id": str(new_bible.id)},
        )
    )
    agent_api.production.status = "waiting_approval"
    agent_api.production.current_stage = "script_review"
    await agent_api.session.commit()

    script_package = await agent_api.client.get(f"{prefix}/script-package")
    assert script_package.status_code == 200
    assert script_package.json()["data"]["bible_version"] == 2
    assert script_package.json()["data"]["status"] == "waiting_review"

    workflow = await agent_api.client.get(f"{prefix}/workflow")
    assert workflow.status_code == 200
    workflow_data = workflow.json()["data"]
    assert workflow_data["current_step"] == 1
    assert workflow_data["steps"][1]["can_view"] is True
    assert workflow_data["steps"][2]["can_view"] is True
    core_assets = await agent_api.client.get(f"{prefix}/core-assets")
    assert core_assets.status_code == 200

    confirmed_package = await agent_api.client.post(
        f"{prefix}/script-package/confirm",
        json={
            "expected_script_version": script_package.json()["data"]["script_version"],
            "expected_bible_version": script_package.json()["data"]["bible_version"],
            "idempotency_key": "supplement-script-package-v2",
        },
    )
    assert confirmed_package.status_code == 200
    assert confirmed_package.json()["data"]["created_chapter_count"] == 1
    await agent_api.session.refresh(agent_api.production)
    chapter_result = await agent_api.session.execute(
        select(ProjectChapter)
        .where(
            ProjectChapter.project_id == agent_api.project.id,
            ProjectChapter.is_enabled.is_(True),
            ProjectChapter.extra["agent_production_id"].as_string()
            == str(agent_api.production.id),
        )
        .order_by(ProjectChapter.sort_order, ProjectChapter.id)
    )
    chapters = list(chapter_result.scalars().all())
    assert len(chapters) == 2
    assert chapters[0].id == agent_api.chapter.id
    assert chapters[0].extra["storyboard_analysis_status"] == "success"
    assert chapters[1].processed_content == "次日，沈砚再次回到旧宅。"
    assert chapters[1].extra["episode_number"] == 2
    assert agent_api.production.extra[
        "pending_incremental_storyboard_chapter_ids"
    ] == [str(chapters[1].id)]


@pytest.mark.asyncio
async def test_agent_storyboard_step_generates_ordered_timed_scripts(
    agent_api,
    monkeypatch,
) -> None:
    story_step = AgentStep(
        id=uuid4(),
        production_id=agent_api.production.id,
        stage="story_bible",
        scope_type="production",
        scope_id=agent_api.production.id,
        status="completed",
        input_version=1,
        output_version=1,
        progress_current=1,
        progress_total=1,
        attempt_count=1,
        extra={},
    )
    core_step = AgentStep(
        id=uuid4(),
        production_id=agent_api.production.id,
        stage="core_assets",
        scope_type="production",
        scope_id=agent_api.production.id,
        status="completed",
        input_version=1,
        output_version=1,
        progress_current=0,
        progress_total=0,
        attempt_count=1,
        extra={},
    )
    bible = SeriesBibleVersion(
        id=uuid4(),
        project_id=agent_api.project.id,
        production_id=agent_api.production.id,
        step_id=story_step.id,
        version=1,
        status="confirmed",
        content={},
        created_by=agent_api.owner.id,
        confirmed_by=agent_api.owner.id,
    )
    character = ProjectCharacter(
        id=uuid4(),
        project_id=agent_api.project.id,
        user_id=agent_api.owner.id,
        name="沈砚",
        aliases=["阿砚"],
        reference_image="https://example.com/shenyan.png",
        extra={},
        is_enabled=True,
    )
    candidate = AgentAssetCandidate(
        id=uuid4(),
        project_id=agent_api.project.id,
        production_id=agent_api.production.id,
        bible_version_id=bible.id,
        user_id=agent_api.owner.id,
        asset_type="character",
        candidate_key="d" * 64,
        canonical_name="沈砚",
        aliases=["阿砚"],
        source_chapter_ids=[str(agent_api.chapter.id)],
        confidence=1,
        merge_reason="标准名称一致",
        review_status="materialized",
        content={},
        materialized_asset_id=character.id,
        lock_version=1,
    )
    variant = AgentAssetVariant(
        id=uuid4(),
        project_id=agent_api.project.id,
        production_id=agent_api.production.id,
        bible_version_id=bible.id,
        base_candidate_id=candidate.id,
        user_id=agent_api.owner.id,
        asset_type="character",
        variant_key="e" * 64,
        canonical_name="沈砚夜行装",
        variant_type="costume",
        description="黑色夜行服",
        trigger_reason="夜探旧宅",
        episode_numbers=[1],
        source_evidence=[],
        confidence=1,
        review_status="ready",
        content={},
        lock_version=1,
    )
    core_lock = AgentCoreAssetLock(
        id=uuid4(),
        project_id=agent_api.project.id,
        production_id=agent_api.production.id,
        bible_version_id=bible.id,
        step_id=core_step.id,
        version=1,
        status="active",
        assets=[
            {
                "asset_type": "character",
                "asset_id": str(character.id),
                "candidate_id": str(candidate.id),
                "name": character.name,
                "reference_image": character.reference_image,
            }
        ],
        impact={},
        idempotency_key="storyboard-core-lock-v1",
        created_by=agent_api.owner.id,
    )
    second_chapter = ProjectChapter(
        id=uuid4(),
        project_id=agent_api.project.id,
        user_id=agent_api.owner.id,
        title="第二集",
        content="沈砚离开旧宅。",
        processed_content="沈砚离开旧宅。",
        process_status="completed",
        sort_order=2,
        extra={
            "agent_production_id": str(agent_api.production.id),
            "episode_number": 2,
        },
        is_enabled=True,
    )
    agent_api.project.project_kind = "agent"
    agent_api.production.current_stage = "batch_production"
    agent_api.production.production_spec = {
        **(agent_api.production.production_spec or {}),
        "workflow_version": 2,
        "pilot_episode_count": 0,
    }
    agent_api.owner.points_balance = 1000
    agent_api.session.add_all([story_step, core_step, character, second_chapter])
    await agent_api.session.flush()
    agent_api.session.add(bible)
    await agent_api.session.flush()
    agent_api.session.add(candidate)
    await agent_api.session.flush()
    agent_api.session.add_all([variant, core_lock])
    await agent_api.session.commit()

    monkeypatch.setattr(
        "app.tasks.project_storyboard.run_project_storyboard_analysis.apply_async",
        lambda **_kwargs: None,
    )
    prefix = f"/api/v1/agent-productions/{agent_api.production.id}/storyboards"
    submitted = await agent_api.client.post(
        f"{prefix}/generations",
        json={
            "expected_core_asset_lock_version": 1,
            "idempotency_key": "storyboard-generation-v1",
        },
    )
    assert submitted.status_code == 200
    data = submitted.json()["data"]
    assert data["phase"] == "storyboards"
    assert data["episodes"] == []
    assert data["completed_episode_count"] == 0
    assert [item["status"] for item in data["episode_analyses"]] == [
        "pending",
        "pending",
    ]
    assert data["current_analysis"]["chapter_id"] == str(agent_api.chapter.id)
    assert data["current_analysis"]["status"] == "pending"
    assert data["active_task_count"] == 2
    assert data["storyboard_count"] == 0

    await agent_api.session.refresh(agent_api.chapter)
    await agent_api.session.refresh(second_chapter)
    task_id = UUID(agent_api.chapter.extra["storyboard_analysis_task_record_id"])
    assert second_chapter.extra["storyboard_analysis_status"] == "pending"
    assert second_chapter.extra["storyboard_analysis_task_record_id"] != str(task_id)
    task = await agent_api.session.get(UserTaskRecord, task_id)
    assert task.extra["prompt_source"] == "agent"
    assert "duration_seconds" in task.prompt
    assert "沈砚夜行装" in task.prompt
    task_result = await agent_api.session.execute(
        select(UserTaskRecord.generation_type).where(
            UserTaskRecord.business_id == agent_api.project.id
        )
    )
    assert set(task_result.scalars().all()) == {"storyboard_analysis"}

    async def run_model(*_args, **_kwargs):
        return ModelRunResult(
            content='''{"storyboard_groups":[
                {"group_number":1,"title":"沈砚发现玉佩","source_content":"沈砚在旧宅发现玉佩。","event_goal":"发现线索","atmosphere":"压抑","production_focus":"空间连续","ending_frame":"沈砚停步","shots":[
                    {"shot_number":7,"shot_size":"全景","camera_shot":"从门外拍摄沈砚进入旧宅","camera_angle":"平视","camera_movement":"缓慢推进","visual_content":"沈砚穿着夜行装推开旧宅木门","scene_name":"旧宅","characters":["沈砚夜行装"],"props":["木门"],"speaker":"","dialogue":""},
                    {"shot_number":2,"shot_size":"近景","camera_shot":"拍摄沈砚警惕观察室内","camera_angle":"侧面","camera_movement":"跟随","visual_content":"沈砚停步并扫视昏暗室内","scene_name":"旧宅","characters":["沈砚夜行装"],"props":[],"speaker":"","dialogue":""}
                ]},
                {"group_number":2,"title":"黑衣人现身","source_content":"黑衣人随后现身。","event_goal":"制造威胁","atmosphere":"悬疑","production_focus":"空间连续","ending_frame":"门口黑影占据画面","shots":[
                    {"shot_number":4,"shot_size":"特写","camera_shot":"拍摄门口掠过的黑影","camera_angle":"平视","camera_movement":"固定","visual_content":"一道黑影从旧宅门口掠过","scene_name":"旧宅","characters":[],"props":[],"speaker":"","dialogue":""}
                ]}
            ]}''',
            extra={},
        )

    monkeypatch.setattr("app.services.project_storyboards.run_model", run_model)
    await run_storyboard_analysis_in_worker(agent_api.session, task, agent_api.chapter)
    await agent_api.session.commit()

    result = await agent_api.client.get(prefix)
    assert result.status_code == 200
    result_data = result.json()["data"]
    assert result_data["completed_episode_count"] == 1
    assert result_data["current_analysis"]["chapter_id"] == str(second_chapter.id)
    episode = result_data["episodes"][0]
    assert [item["shot_number"] for item in episode["storyboards"]] == [1, 2]
    assert [item["estimated_duration_seconds"] for item in episode["storyboards"]] == [4, 4]
    assert [len(item["shots"]) for item in episode["storyboards"]] == [2, 1]
    assert episode["estimated_duration_seconds"] == 8
    first_group = episode["storyboards"][0]
    assert first_group["storyboard_prompt"].startswith("画面风格：写实漫剧风格")
    assert "视频中不得出现任何字幕、文字叠加" in first_group["storyboard_prompt"]
    assert "不要BGM，不要配乐" in first_group["storyboard_prompt"]
    assert "镜头1：景别：全景" in first_group["storyboard_prompt"]
    assert "镜头2：景别：近景" in first_group["storyboard_prompt"]
    assert first_group["storyboard_prompt"].endswith("分镜组总时长：4秒")
    assert first_group["asset_ids"]["character"] == [str(character.id)]
    assert first_group["group_number"] == 1
    assert first_group["revision"] == 1
    assert first_group["status"] == "ready"
    assert first_group["asset_bindings"] == [
        {
            "binding_key": "character_1",
            "asset_type": "character",
            "asset_id": str(character.id),
            "variant_id": str(variant.id),
        }
    ]
    assert first_group["shots"][0]["character_binding_keys"] == ["character_1"]

    stored_group = await agent_api.session.get(ProjectStoryboard, UUID(first_group["id"]))
    stored_group.extra = {
        **(stored_group.extra or {}),
        "agent_asset_ids": {"character": [], "scene": [], "prop": []},
        "agent_unbound_asset_names": {
            "character": ["沈砚夜行装"],
            "scene": [],
            "prop": [],
        },
        "agent_asset_binding_version": 1,
    }
    await agent_api.session.commit()

    refreshed = await agent_api.client.get(prefix)
    refreshed_group = refreshed.json()["data"]["episodes"][0]["storyboards"][0]
    assert refreshed_group["asset_ids"]["character"] == []

    await agent_api.session.refresh(stored_group)
    assert stored_group.extra["agent_asset_binding_version"] == 1
    stored_group.extra = {
        **(stored_group.extra or {}),
        "image_generation_status": "success",
        "video_generation_status": "success",
    }
    image_history = ProjectGeneratedAsset(
        id=uuid4(),
        project_id=agent_api.project.id,
        chapter_id=agent_api.chapter.id,
        user_id=agent_api.owner.id,
        target_type="storyboard",
        target_id=stored_group.id,
        media_type="image",
        result_url="https://example.com/storyboard.png",
        result_urls=["https://example.com/storyboard.png"],
        status="success",
        is_selected=True,
        extra={},
        is_enabled=True,
    )
    video_history = ProjectGeneratedAsset(
        id=uuid4(),
        project_id=agent_api.project.id,
        chapter_id=agent_api.chapter.id,
        user_id=agent_api.owner.id,
        target_type="storyboard",
        target_id=stored_group.id,
        media_type="video",
        result_url="https://example.com/storyboard.mp4",
        result_urls=["https://example.com/storyboard.mp4"],
        status="success",
        is_selected=True,
        extra={},
        is_enabled=True,
    )
    agent_api.session.add_all([image_history, video_history])
    await agent_api.session.commit()

    edited_prompt = f"{refreshed_group['storyboard_prompt']}\n剪辑要求：结尾停留在人物表情。"
    edited = await agent_api.client.patch(
        f"{prefix}/{refreshed_group['id']}",
        json={
            "expected_core_asset_lock_version": 1,
            "expected_revision": refreshed_group["revision"],
            "storyboard_prompt": edited_prompt,
            "asset_bindings": [
                {
                    "asset_type": "character",
                    "asset_id": str(character.id),
                    "variant_id": str(variant.id),
                }
            ],
        },
    )
    assert edited.status_code == 200
    edited_data = edited.json()["data"]
    assert edited_data["storyboard_prompt"] == edited_prompt
    assert edited_data["asset_bindings"] == [
        {
            "binding_key": "character_1",
            "asset_type": "character",
            "asset_id": str(character.id),
            "variant_id": str(variant.id),
        }
    ]

    await agent_api.session.refresh(stored_group)
    await agent_api.session.refresh(image_history)
    await agent_api.session.refresh(video_history)
    assert stored_group.extra["image_generation_status"] == "invalidated"
    assert stored_group.extra["video_generation_status"] == "invalidated"
    assert image_history.extra["validity_status"] == "invalidated"
    assert video_history.extra["validity_status"] == "invalidated"
    await bind_storyboards_to_core_lock(
        agent_api.session,
        agent_api.production,
        core_lock,
        [stored_group],
    )
    assert stored_group.extra["agent_asset_binding_source"] == "user"
    assert stored_group.extra["agent_asset_variant_ids"]["character"][str(character.id)] == str(
        variant.id
    )
    assert stored_group.extra["agent_asset_variant_context"][0]["name"] == "沈砚夜行装"

    base_binding = await agent_api.client.patch(
        f"{prefix}/{refreshed_group['id']}",
        json={
            "expected_core_asset_lock_version": 1,
            "expected_revision": edited_data["revision"],
            "asset_bindings": [
                {
                    "binding_key": "character_1",
                    "asset_type": "character",
                    "asset_id": str(character.id),
                }
            ],
        },
    )
    assert base_binding.status_code == 200
    base_data = base_binding.json()["data"]
    assert base_data["revision"] == edited_data["revision"] + 1
    assert base_data["characters"] == ["沈砚"]
    assert "沈砚夜行装穿着夜行装" not in base_data["storyboard_prompt"]
    assert "沈砚穿着夜行装" in base_data["storyboard_prompt"]

    stale = await agent_api.client.patch(
        f"{prefix}/{refreshed_group['id']}",
        json={
            "expected_core_asset_lock_version": 1,
            "expected_revision": edited_data["revision"],
            "prompt_notes": "这个保存请求已经过期",
        },
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == 40959

    removed = await agent_api.client.patch(
        f"{prefix}/{refreshed_group['id']}",
        json={
            "expected_core_asset_lock_version": 1,
            "expected_revision": base_data["revision"],
            "asset_bindings": [],
        },
    )
    assert removed.status_code == 200
    assert removed.json()["data"]["asset_bindings"] == []
    assert removed.json()["data"]["asset_ids"] == {
        "character": [],
        "scene": [],
        "prop": [],
    }
    removed_data = removed.json()["data"]
    assert "{{asset:" not in removed_data["storyboard_prompt"]

    copied = await agent_api.client.post(
        f"{prefix}/{refreshed_group['id']}/copy",
        json={
            "expected_core_asset_lock_version": 1,
            "expected_revision": removed_data["revision"],
        },
    )
    assert copied.status_code == 200
    copied_data = copied.json()["data"]
    assert copied_data["origin"] == "copy"
    assert copied_data["revision"] == 1

    package_after_copy = (await agent_api.client.get(prefix)).json()["data"]
    episode_after_copy = package_after_copy["episodes"][0]
    created = await agent_api.client.post(
        f"/api/v1/agent-productions/{agent_api.production.id}"
        f"/episodes/{agent_api.chapter.id}/storyboards",
        json={
            "expected_core_asset_lock_version": 1,
            "expected_episode_revision": episode_after_copy["revision"],
            "insert_after_storyboard_id": copied_data["id"],
            "title": "补充反应镜头",
        },
    )
    assert created.status_code == 200
    await agent_api.session.refresh(agent_api.owner)
    assert agent_api.owner.points_balance == 986
    created_data = created.json()["data"]
    assert created_data["origin"] == "user"
    assert created_data["status"] == "draft"

    autosaved = await agent_api.client.patch(
        f"{prefix}/{created_data['id']}",
        json={
            "expected_core_asset_lock_version": 1,
            "expected_revision": created_data["revision"],
            "prompt_notes": "加强人物听到动静后的紧张反应",
            "shots": [
                {
                    "shot_number": 1,
                    "shot_size": "近景",
                    "camera_shot": "拍摄人物侧脸",
                    "camera_angle": "平视",
                    "camera_movement": "缓慢推进",
                    "visual_content": "人物突然停下并回头",
                    "scene_name": "",
                    "characters": [],
                    "props": [],
                    "speaker": "",
                    "dialogue": "",
                }
            ],
        },
    )
    assert autosaved.status_code == 200
    autosaved_data = autosaved.json()["data"]
    assert autosaved_data["status"] == "ready"
    assert autosaved_data["revision"] == created_data["revision"] + 1
    assert "补充要求：加强人物听到动静后的紧张反应" in autosaved_data[
        "effective_prompt"
    ]

    package_after_create = (await agent_api.client.get(prefix)).json()["data"]
    episode_after_create = package_after_create["episodes"][0]
    reversed_ids = [
        item["id"] for item in reversed(episode_after_create["storyboards"])
    ]
    reordered = await agent_api.client.put(
        f"/api/v1/agent-productions/{agent_api.production.id}"
        f"/episodes/{agent_api.chapter.id}/storyboards/order",
        json={
            "expected_episode_revision": episode_after_create["revision"],
            "storyboard_ids": reversed_ids,
        },
    )
    assert reordered.status_code == 200
    assert [item["id"] for item in reordered.json()["data"]["storyboards"]] == reversed_ids

    created_after_reorder = next(
        item
        for item in reordered.json()["data"]["storyboards"]
        if item["id"] == created_data["id"]
    )
    deleted = await agent_api.client.delete(
        f"{prefix}/{created_data['id']}",
        params={
            "expected_core_asset_lock_version": 1,
            "expected_revision": created_after_reorder["revision"],
        },
    )
    assert deleted.status_code == 200
    assert deleted.json()["data"]["deleted"] is True

    remaining = (await agent_api.client.get(prefix)).json()["data"]["episodes"][0][
        "storyboards"
    ]
    while len(remaining) > 1:
        removable = remaining[0]
        response = await agent_api.client.delete(
            f"{prefix}/{removable['id']}",
            params={
                "expected_core_asset_lock_version": 1,
                "expected_revision": removable["revision"],
            },
        )
        assert response.status_code == 200
        remaining = (await agent_api.client.get(prefix)).json()["data"]["episodes"][0][
            "storyboards"
        ]

    last = remaining[0]
    blocked_last_delete = await agent_api.client.delete(
        f"{prefix}/{last['id']}",
        params={
            "expected_core_asset_lock_version": 1,
            "expected_revision": last["revision"],
        },
    )
    assert blocked_last_delete.status_code == 409
    assert blocked_last_delete.json()["code"] == 40969


@pytest.mark.asyncio
async def test_story_bible_http_flow_enforces_review_idempotency_and_ownership(agent_api) -> None:
    prefix = f"/api/v1/agent-productions/{agent_api.production.id}"

    initialized = await agent_api.client.post(f"{prefix}/story-bible/initialize")
    assert initialized.status_code == 200
    assert initialized.json()["data"]["version"] == 1

    listed = await agent_api.client.get(f"{prefix}/asset-candidates")
    assert listed.status_code == 200
    candidates = listed.json()["data"]["items"]
    low_confidence = next(item for item in candidates if item["review_status"] == "needs_review")

    blocked = await agent_api.client.post(
        f"{prefix}/asset-candidates/materialize",
        json={"expected_bible_version": 1, "candidate_ids": [low_confidence["id"]]},
    )
    assert blocked.status_code == 409

    reviewed = await agent_api.client.patch(
        f"{prefix}/asset-candidates/{low_confidence['id']}",
        json={"expected_lock_version": 0, "review_status": "ready"},
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["data"]["lock_version"] == 1

    materialized = await agent_api.client.post(
        f"{prefix}/asset-candidates/materialize",
        json={"expected_bible_version": 1, "candidate_ids": [low_confidence["id"]]},
    )
    repeated = await agent_api.client.post(
        f"{prefix}/asset-candidates/materialize",
        json={"expected_bible_version": 1, "candidate_ids": [low_confidence["id"]]},
    )
    assert materialized.json()["data"]["created_count"] == 1
    assert repeated.json()["data"]["created_count"] == 0
    assert repeated.json()["data"]["reused_count"] == 1

    characters = await agent_api.client.get(f"/api/v1/projects/{agent_api.project.id}/characters")
    assert characters.status_code == 200
    assert characters.json()["data"]["total"] == 1

    confirmed = await agent_api.client.post(
        f"{prefix}/story-bible/confirm",
        json={"expected_version": 1, "idempotency_key": "integration-confirm-v1"},
    )
    confirmed_again = await agent_api.client.post(
        f"{prefix}/story-bible/confirm",
        json={"expected_version": 1, "idempotency_key": "integration-confirm-v1"},
    )
    assert confirmed.json()["data"]["already_confirmed"] is False
    assert confirmed_again.json()["data"]["already_confirmed"] is True

    agent_api.identity["user"] = agent_api.outsider
    forbidden = await agent_api.client.get(f"{prefix}/story-bible")
    assert forbidden.status_code == 404
    assert forbidden.json()["code"] == 40430


@pytest.mark.asyncio
async def test_agent_production_rejects_mismatched_project_owner(agent_api) -> None:
    outsider_project = Project(
        id=uuid4(),
        user_id=agent_api.outsider.id,
        style_id=agent_api.style.id,
        name="其他用户的 Agent 项目",
        cover="https://example.com/outsider-cover.png",
        description="ownership boundary",
        generation_ratio="16:9",
        project_kind="agent",
        is_enabled=True,
    )
    outsider_source = ProjectSourceDocument(
        id=uuid4(),
        project_id=outsider_project.id,
        user_id=agent_api.outsider.id,
        source_type="text",
        content="其他用户的剧本。",
        content_hash=uuid4().hex * 2,
        character_count=8,
        version=1,
        parse_status="success",
        extra={},
    )
    mismatched_production = AgentProduction(
        id=uuid4(),
        project_id=outsider_project.id,
        user_id=agent_api.owner.id,
        source_document_id=outsider_source.id,
        status="planning",
        current_stage="story_bible",
        mode="supervised",
        production_spec={},
        estimated_points=0,
        consumed_points=0,
        lock_version=0,
        extra={},
    )
    agent_api.session.add_all(
        [outsider_project, outsider_source, mismatched_production]
    )
    await agent_api.session.commit()

    response = await agent_api.client.get(
        f"/api/v1/agent-productions/{mismatched_production.id}"
    )

    assert response.status_code == 404
    assert response.json()["code"] == 40430


@pytest.mark.asyncio
async def test_agent_production_rejects_source_from_another_project(agent_api) -> None:
    outsider_project = Project(
        id=uuid4(),
        user_id=agent_api.outsider.id,
        style_id=agent_api.style.id,
        name="其他用户的剧本项目",
        cover="https://example.com/outsider-source-cover.png",
        description="source ownership boundary",
        generation_ratio="16:9",
        project_kind="agent",
        is_enabled=True,
    )
    outsider_source = ProjectSourceDocument(
        id=uuid4(),
        project_id=outsider_project.id,
        user_id=agent_api.outsider.id,
        source_type="text",
        content="不能被当前用户读取的剧本。",
        content_hash=uuid4().hex * 2,
        character_count=13,
        version=1,
        parse_status="success",
        extra={},
    )
    mismatched_production = AgentProduction(
        id=uuid4(),
        project_id=agent_api.project.id,
        user_id=agent_api.owner.id,
        source_document_id=outsider_source.id,
        status="planning",
        current_stage="story_bible",
        mode="supervised",
        production_spec={},
        estimated_points=0,
        consumed_points=0,
        lock_version=0,
        extra={},
    )
    agent_api.session.add_all(
        [outsider_project, outsider_source, mismatched_production]
    )
    await agent_api.session.commit()

    response = await agent_api.client.get(
        f"/api/v1/agent-productions/{mismatched_production.id}"
    )

    assert response.status_code == 404
    assert response.json()["code"] == 40430


@pytest.mark.asyncio
async def test_agent_http_surfaces_hide_owner_production_from_outsider(agent_api) -> None:
    agent_api.identity["user"] = agent_api.outsider
    prefix = f"/api/v1/agent-productions/{agent_api.production.id}"

    for path in (
        prefix,
        f"{prefix}/workbench",
        f"{prefix}/workflow",
        f"{prefix}/script-package",
        f"{prefix}/core-assets",
        f"{prefix}/storyboards",
        f"{prefix}/review",
        f"{prefix}/matrix",
        f"{prefix}/events",
        f"{prefix}/costs",
    ):
        response = await agent_api.client.get(path)
        assert response.status_code == 404, path
        assert response.json()["code"] == 40430, path


@pytest.mark.asyncio
async def test_script_package_flow_materializes_text_assets_without_reference_images(
    agent_api,
) -> None:
    source = await agent_api.session.get(
        ProjectSourceDocument,
        agent_api.production.source_document_id,
    )
    production = AgentProduction(
        id=uuid4(),
        project_id=agent_api.project.id,
        user_id=agent_api.owner.id,
        source_document_id=source.id,
        status="running",
        current_stage="source_analysis",
        mode="supervised",
        production_spec={
            "workflow_version": 2,
            "text_model_id": str(agent_api.text_model.id),
            "image_model_id": str(agent_api.image_model.id),
        },
        estimated_points=0,
        consumed_points=0,
        lock_version=0,
        extra={},
    )
    episode_plan = {
        "planning_summary": "单集结构",
        "episodes": [
            {
                "episode_number": 1,
                "title": "旧宅寻物",
                "content": source.content,
                "opening_hook": "沈砚进入旧宅",
                "goal": "寻找玉佩",
                "conflict": "黑衣人阻拦",
                "climax": "双方对峙",
                "ending_hook": "玉佩突然碎裂",
                "estimated_duration_seconds": 90,
                "estimated_shot_count": 18,
                "source_start": 0,
                "source_end": len(source.content),
                "characters": ["沈砚"],
                "character_variants": ["沈砚受伤造型"],
                "scenes": ["旧宅"],
                "scene_variants": ["雨夜旧宅"],
                "props": ["玉佩"],
                "prop_variants": ["碎裂玉佩"],
                "continuity_notes": [],
            }
        ],
    }
    global_analysis = {
        "story_summary": "沈砚在旧宅寻找玉佩",
        "characters": [{"name": "沈砚", "aliases": ["阿砚"], "confidence": 1}],
        "character_variants": [
            {
                "base_name": "沈砚",
                "name": "沈砚受伤造型",
                "variant_type": "injury",
                "description": "额角流血",
                "trigger_reason": "遭到黑衣人袭击",
                "source_start": 0,
                "source_end": len(source.content),
                "confidence": 1,
            }
        ],
        "scenes": [{"name": "旧宅", "confidence": 1}],
        "scene_variants": [
            {
                "base_name": "旧宅",
                "name": "雨夜旧宅",
                "variant_type": "weather",
                "description": "暴雨中的旧宅",
                "trigger_reason": "故事发生在雨夜",
                "source_start": 0,
                "source_end": len(source.content),
                "confidence": 1,
            }
        ],
        "prop_variants": [
            {
                "base_name": "玉佩",
                "name": "碎裂玉佩",
                "variant_type": "damage",
                "description": "裂成两半",
                "trigger_reason": "对峙中被击碎",
                "source_start": 0,
                "source_end": len(source.content),
                "confidence": 1,
            }
        ],
    }
    step = AgentStep(
        id=uuid4(),
        production_id=production.id,
        stage="source_analysis",
        scope_type="production",
        scope_id=production.id,
        status="running",
        input_version=1,
        output_version=1,
        progress_current=3,
        progress_total=3,
        attempt_count=1,
        extra={},
    )
    agent_api.session.add_all([production, step])
    await agent_api.session.flush()
    source.parse_status = "running"
    await _complete_source_analysis(
        agent_api.session,
        production,
        step,
        source,
        SimpleNamespace(id=uuid4(), extra={"parsed_result": global_analysis}),
        SimpleNamespace(id=uuid4(), extra={"parsed_result": episode_plan}),
    )
    await agent_api.session.commit()
    bible = (
        await agent_api.session.execute(
            select(SeriesBibleVersion).where(
                SeriesBibleVersion.production_id == production.id
            )
        )
    ).scalar_one()
    candidates = (
        await agent_api.session.execute(
            select(AgentAssetCandidate).where(
                AgentAssetCandidate.production_id == production.id
            )
        )
    ).scalars().all()
    variants = (
        await agent_api.session.execute(
            select(AgentAssetVariant).where(
                AgentAssetVariant.production_id == production.id
            )
        )
    ).scalars().all()
    assert len(candidates) == 3
    assert len(variants) == 3
    assert step.extra["script_warnings"] == []
    assert production.status == "waiting_approval"
    assert production.current_stage == "script_review"

    prefix = f"/api/v1/agent-productions/{production.id}"
    package = await agent_api.client.get(f"{prefix}/script-package")
    assert package.status_code == 200
    package_data = package.json()["data"]
    assert package_data["status"] == "waiting_review"
    assert len(package_data["episodes"]) == 1
    assert package_data["episodes"][0]["episode_number"] == 1
    assert package_data["episodes"][0]["source_content"] == source.content
    assert len(package_data["characters"]) == 1
    assert package_data["characters"][0]["episode_numbers"] == [1]
    assert len(package_data["character_variants"]) == 1
    assert package_data["character_variants"][0]["episode_numbers"] == [1]
    assert len(package_data["scenes"]) == 1
    assert package_data["scenes"][0]["episode_numbers"] == [1]
    assert len(package_data["scene_variants"]) == 1
    assert package_data["scene_variants"][0]["episode_numbers"] == [1]
    assert len(package_data["props"]) == 1
    assert package_data["props"][0]["episode_numbers"] == [1]
    assert len(package_data["prop_variants"]) == 1
    assert package_data["prop_variants"][0]["episode_numbers"] == [1]
    assert package_data["props"][0]["review_status"] == "needs_review"

    legacy_episode_confirm = await agent_api.client.post(
        f"{prefix}/episode-plans/confirm",
        json={
            "expected_version": package_data["script_version"],
            "idempotency_key": "legacy-episode-confirm",
        },
    )
    legacy_bible_confirm = await agent_api.client.post(
        f"{prefix}/story-bible/confirm",
        json={
            "expected_version": package_data["bible_version"],
            "idempotency_key": "legacy-bible-confirm",
        },
    )
    assert legacy_episode_confirm.status_code == 409
    assert legacy_episode_confirm.json()["code"] == 40947
    assert legacy_bible_confirm.status_code == 409
    assert legacy_bible_confirm.json()["code"] == 40947

    variant = package_data["character_variants"][0]
    updated = await agent_api.client.patch(
        f"{prefix}/asset-variants/{variant['id']}",
        json={
            "expected_lock_version": variant["lock_version"],
            "canonical_name": "沈砚战损造型",
            "description": "额角与衣领均有血迹",
        },
    )
    assert updated.status_code == 200
    assert updated.json()["data"]["description"] == "额角与衣领均有血迹"
    stale_confirm = await agent_api.client.post(
        f"{prefix}/script-package/confirm",
        json={
            "expected_script_version": package_data["script_version"],
            "expected_bible_version": package_data["bible_version"],
            "idempotency_key": "script-package-integration-v1",
        },
    )
    assert stale_confirm.status_code == 409
    assert stale_confirm.json()["code"] == 40941
    refreshed_package = (await agent_api.client.get(f"{prefix}/script-package")).json()["data"]
    assert refreshed_package["episodes"][0]["character_variants"] == ["沈砚战损造型"]

    confirmed = await agent_api.client.post(
        f"{prefix}/script-package/confirm",
        json={
            "expected_script_version": refreshed_package["script_version"],
            "expected_bible_version": refreshed_package["bible_version"],
            "idempotency_key": "script-package-integration-v1",
        },
    )
    repeated = await agent_api.client.post(
        f"{prefix}/script-package/confirm",
        json={
            "expected_script_version": refreshed_package["script_version"],
            "expected_bible_version": refreshed_package["bible_version"],
            "idempotency_key": "script-package-integration-v1",
        },
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["data"]["created_chapter_count"] == 1
    assert confirmed.json()["data"]["created_asset_count"] == 3
    assert repeated.json()["data"]["already_confirmed"] is True
    conflicting_replay = await agent_api.client.post(
        f"{prefix}/script-package/confirm",
        json={
            "expected_script_version": refreshed_package["script_version"],
            "expected_bible_version": refreshed_package["bible_version"],
            "idempotency_key": "script-package-other-key",
        },
    )
    assert conflicting_replay.status_code == 409
    assert conflicting_replay.json()["code"] == 40948

    await agent_api.session.refresh(production)
    assert production.status == "planning"
    assert production.current_stage == "core_assets"
    assert bible.status == "confirmed"
    stored_variants = (
        await agent_api.session.execute(
            select(AgentAssetVariant).where(
                AgentAssetVariant.production_id == production.id
            )
        )
    ).scalars().all()
    assert {item.asset_type for item in stored_variants} == {
        "character",
        "scene",
        "prop",
    }
    characters = (
        await agent_api.session.execute(
            select(ProjectCharacter).where(
                ProjectCharacter.project_id == agent_api.project.id
            )
        )
    ).scalars().all()
    scenes = (
        await agent_api.session.execute(
            select(ProjectScene).where(ProjectScene.project_id == agent_api.project.id)
        )
    ).scalars().all()
    props = (
        await agent_api.session.execute(
            select(ProjectProp).where(ProjectProp.project_id == agent_api.project.id)
        )
    ).scalars().all()
    assert all(item.reference_image is None for item in [*characters, *scenes, *props])


@pytest.mark.asyncio
async def test_source_file_preview_can_create_agent_production(agent_api, monkeypatch) -> None:
    async def fake_upload(_file, category=""):
        assert category == f"agent-source/{agent_api.project.id}"
        return UploadFileOut(
            url="https://example.com/agent-source/script.txt",
            object_key="story/agent-source/script.txt",
            filename="整剧.txt",
            content_type="text/plain",
            size=40,
            file_type="file",
        )

    monkeypatch.setattr("app.services.agent_source_files.upload_story_file", fake_upload)
    preview = await agent_api.client.post(
        f"/api/v1/projects/{agent_api.project.id}/agent-productions/source-preview",
        files={"file": ("整剧.txt", "第一集：雨夜相遇\n第二集：旧宅重逢".encode(), "text/plain")},
    )

    assert preview.status_code == 200
    preview_data = preview.json()["data"]
    assert preview_data["source_type"] == "txt"
    assert preview_data["parse_status"] == "previewed"
    assert preview_data["content_preview"] == "第一集：雨夜相遇\n第二集：旧宅重逢"
    assert preview_data["content_preview_truncated"] is False

    created = await agent_api.client.post(
        f"/api/v1/projects/{agent_api.project.id}/agent-productions",
        json={
            "source_document_id": preview_data["id"],
            "production_spec": {
                "text_model_id": str(agent_api.text_model.id),
                "image_model_id": str(agent_api.image_model.id),
                "video_model_id": str(agent_api.video_model.id),
            },
        },
    )
    assert created.status_code == 200
    created_data = created.json()["data"]
    assert created_data["source_document_id"] == preview_data["id"]
    assert created_data["source_document"]["file_name"] == "整剧.txt"

    workbench = await agent_api.client.get(
        f"/api/v1/agent-productions/{created_data['id']}/workbench"
    )
    assert workbench.status_code == 200
    workbench_data = workbench.json()["data"]
    assert workbench_data["production"]["id"] == created_data["id"]
    assert workbench_data["matrix"]["total_episode_count"] == 0
    assert workbench_data["costs"]["net_points"] == 0
    assert workbench_data["controller"] is None
    assert workbench_data["next_action"] == "start"


@pytest.mark.asyncio
async def test_script_first_entries_create_isolated_agent_projects_and_snapshot_defaults(
    agent_api,
    monkeypatch,
) -> None:
    async def skip_enqueue(_db, _production_id, _step_id):
        return None

    async def fake_upload(_file, category=""):
        assert category.startswith("agent-source/")
        return UploadFileOut(
            url="https://example.com/agent-source/script.txt",
            object_key="story/agent-source/script.txt",
            filename="文件入口.txt",
            content_type="text/plain",
            size=30,
            file_type="file",
        )

    monkeypatch.setattr(
        "app.services.agent_productions._enqueue_source_analysis",
        skip_enqueue,
    )
    monkeypatch.setattr("app.services.agent_entries.upload_story_file", fake_upload)

    automatic_payload = {
        "name": "新契约整剧",
        "content": "第一集：雨夜相遇\n第二集：旧宅重逢",
        "style_id": str(agent_api.project.style_id),
        "generation_ratio": "9:16",
        "video_resolution": "1080p",
        "mode": "automatic",
        "video_model_id": str(agent_api.video_model.id),
    }
    agent_api.owner.points_balance = 99
    await agent_api.session.commit()
    insufficient_creation = await agent_api.client.post(
        "/api/v1/agent-productions/from-text",
        json=automatic_payload,
    )
    assert insufficient_creation.status_code == 400
    assert insufficient_creation.json()["code"] == 40003

    agent_api.owner.points_balance = 100
    await agent_api.session.commit()
    created = await agent_api.client.post(
        "/api/v1/agent-productions/from-text",
        json=automatic_payload,
    )
    assert created.status_code == 200
    created_data = created.json()["data"]
    assert created_data["name"] == "新契约整剧"
    assert created_data["status"] == "draft"
    assert created_data["style_id"] == str(agent_api.project.style_id)
    assert created_data["generation_ratio"] == "9:16"
    assert created_data["video_resolution"] == "1080p"
    assert created_data["mode"] == "automatic"
    assert created_data["video_model_id"] == str(agent_api.video_model.id)
    assert created_data["configuration_required"] is False
    assert "project_id" not in created_data
    production_id = UUID(created_data["production_id"])

    configuration = await agent_api.client.get(
        f"/api/v1/agent-productions/{production_id}/configuration"
    )
    assert configuration.status_code == 200
    assert configuration.json()["data"] == {
        "production_id": str(production_id),
        "style_id": str(agent_api.project.style_id),
        "style": {
                "id": str(agent_api.style.id),
                "name": agent_api.style.name,
                "cover": agent_api.style.cover,
                "version": agent_api.style.version,
            },
        "generation_ratio": "9:16",
        "video_resolution": "1080p",
        "mode": "automatic",
        "video_model_id": str(agent_api.video_model.id),
        "configured": True,
        "configurable": True,
    }

    agent_api.owner.points_balance = 0
    await agent_api.session.commit()
    created_file = await agent_api.client.post(
        "/api/v1/agent-productions/from-file",
        data={
            "style_id": str(agent_api.project.style_id),
            "generation_ratio": "16:9",
            "video_resolution": "720p",
            "mode": "supervised",
        },
        files={"file": ("文件入口.txt", "第一集：文件入口".encode(), "text/plain")},
    )
    assert created_file.status_code == 200
    assert created_file.json()["data"]["name"] == "文件入口"
    agent_api.owner.points_balance = 100
    await agent_api.session.commit()

    production = await agent_api.session.get(AgentProduction, production_id)
    assert production is not None
    workflow_states = (
        await agent_api.session.execute(
            select(AgentWorkflowStepState).where(
                AgentWorkflowStepState.production_id == production_id,
                AgentWorkflowStepState.scope_type == "production",
            ).order_by(AgentWorkflowStepState.step_number)
        )
    ).scalars().all()
    assert [(state.step_number, state.status) for state in workflow_states] == [
        (1, "not_started"),
        (2, "not_started"),
        (3, "not_started"),
        (4, "not_started"),
    ]
    internal_project = await agent_api.session.get(Project, production.project_id)
    assert internal_project is not None
    assert internal_project.project_kind == "agent"
    assert internal_project.style_id == agent_api.project.style_id
    assert internal_project.generation_ratio == "9:16"
    assert production.max_points is None
    assert production.production_spec["workflow_version"] == 2
    assert production.production_spec["retry_limit"] == 0
    assert production.production_spec["pilot_episode_count"] == 0
    assert production.production_spec["video_resolution"] == "1080p"
    assert "target_episode_count" not in production.production_spec
    assert "target_episode_duration_seconds" not in production.production_spec
    assert "default_shot_duration_seconds" not in production.production_spec
    assert "text_model_id" not in production.production_spec
    assert production.production_spec["video_model_id"] == str(agent_api.video_model.id)
    assert production.production_spec["video_model_snapshot"] == {
        "record_id": str(agent_api.video_model.id),
        "model_id": agent_api.video_model.model_id,
        "nickname": agent_api.video_model.nickname,
        "vendor": agent_api.video_model.vendor,
    }

    project_center = await agent_api.client.get("/api/v1/projects")
    assert project_center.status_code == 200
    assert {item["id"] for item in project_center.json()["data"]["items"]} == {
        str(agent_api.project.id)
    }
    hidden_project = await agent_api.client.get(f"/api/v1/projects/{internal_project.id}")
    assert hidden_project.status_code == 404

    agent_projects = await agent_api.client.get("/api/v1/agent-productions")
    assert agent_projects.status_code == 200
    agent_project_items = agent_projects.json()["data"]["items"]
    names = {item["name"] for item in agent_project_items}
    assert {"新契约整剧", "文件入口"} <= names
    created_item = next(
        item for item in agent_project_items if item["id"] == str(production_id)
    )
    assert created_item["active_task_count"] == 0
    assert created_item["has_active_tasks"] is False
    assert created_item["should_poll"] is False
    assert created_item["next_poll_seconds"] is None

    active_task = UserTaskRecord(
        id=uuid4(),
        user_id=agent_api.owner.id,
        ai_model_id=agent_api.text_model.id,
        business_type="project",
        business_id=production.project_id,
        generation_type="source_analysis",
        status="pending",
        title="Agent 列表轮询测试",
        prompt="test",
        points_cost=0,
        extra={"agent_production_id": str(production_id)},
    )
    agent_api.session.add(active_task)
    await agent_api.session.commit()
    active_projects = await agent_api.client.get("/api/v1/agent-productions")
    active_item = next(
        item
        for item in active_projects.json()["data"]["items"]
        if item["id"] == str(production_id)
    )
    assert active_item["active_task_count"] == 1
    assert active_item["has_active_tasks"] is True
    assert active_item["should_poll"] is True
    assert active_item["next_poll_seconds"] == 10
    active_task.status = "success"
    await agent_api.session.commit()

    workbench = await agent_api.client.get(
        f"/api/v1/agent-productions/{production_id}/workbench"
    )
    assert workbench.status_code == 200
    assert workbench.json()["data"]["next_action"] == "start"

    agent_api.owner.points_balance = 99
    await agent_api.session.commit()
    insufficient_start = await agent_api.client.post(
        f"/api/v1/agent-productions/{production_id}/start"
    )
    assert insufficient_start.status_code == 400
    assert insufficient_start.json()["code"] == 40003
    await agent_api.session.refresh(production)
    assert production.status == "draft"

    agent_api.owner.points_balance = 100
    agent_api.image_model.is_enabled = False
    await agent_api.session.commit()
    missing_default = await agent_api.client.post(
        f"/api/v1/agent-productions/{production_id}/start"
    )
    assert missing_default.status_code == 409
    assert missing_default.json()["code"] == 40980
    assert missing_default.json()["data"]["missing_models"] == ["image:gpt-image-2"]

    agent_api.image_model.is_enabled = True
    await agent_api.session.commit()
    started = await agent_api.client.post(f"/api/v1/agent-productions/{production_id}/start")
    assert started.status_code == 200
    await agent_api.session.refresh(production)
    assert production.current_stage == "source_analysis"
    assert production.production_spec["text_model_id"] == str(agent_api.text_model.id)
    assert production.production_spec["image_model_id"] == str(agent_api.image_model.id)
    assert production.production_spec["video_model_id"] == str(agent_api.video_model.id)


@pytest.mark.asyncio
async def test_agent_project_delete_soft_deletes_internal_project_and_cancels_work(
    agent_api,
    monkeypatch,
) -> None:
    async def skip_enqueue(_db, _production_id, _step_id):
        return None

    monkeypatch.setattr(
        "app.services.agent_productions._enqueue_source_analysis",
        skip_enqueue,
    )
    created = await agent_api.client.post(
        "/api/v1/agent-productions/from-text",
        json={
            "name": "待删除 Agent 项目",
            "content": "第一集：雨夜相遇",
            "style_id": str(agent_api.project.style_id),
            "generation_ratio": "9:16",
            "video_resolution": "720p",
            "mode": "supervised",
        },
    )
    assert created.status_code == 200
    production_id = UUID(created.json()["data"]["production_id"])

    started = await agent_api.client.post(
        f"/api/v1/agent-productions/{production_id}/start"
    )
    assert started.status_code == 200
    assert started.json()["data"]["status"] == "planning"

    deleted = await agent_api.client.delete(
        f"/api/v1/agent-productions/{production_id}"
    )
    assert deleted.status_code == 200
    assert deleted.json()["data"] == {
        "production_id": str(production_id),
        "status": "cancelled",
        "deleted": True,
    }

    production = await agent_api.session.get(AgentProduction, production_id)
    assert production is not None
    await agent_api.session.refresh(production)
    assert production.status == "cancelled"
    internal_project = await agent_api.session.get(Project, production.project_id)
    assert internal_project is not None
    await agent_api.session.refresh(internal_project)
    assert internal_project.is_enabled is False

    listed = await agent_api.client.get("/api/v1/agent-productions")
    assert listed.status_code == 200
    listed_ids = {item["id"] for item in listed.json()["data"]["items"]}
    assert str(production_id) not in listed_ids

    hidden = await agent_api.client.get(f"/api/v1/agent-productions/{production_id}")
    repeated = await agent_api.client.delete(
        f"/api/v1/agent-productions/{production_id}"
    )
    assert hidden.status_code == 404
    assert hidden.json()["code"] == 40430
    assert repeated.status_code == 404
    assert repeated.json()["code"] == 40430


@pytest.mark.asyncio
async def test_source_analysis_recovery_redispatches_pending_task_without_recharging(
    agent_api,
) -> None:
    source = ProjectSourceDocument(
        id=uuid4(),
        project_id=agent_api.project.id,
        user_id=agent_api.owner.id,
        source_type="text",
        content="雨夜，沈砚走进旧宅。",
        content_hash="b" * 64,
        character_count=11,
        version=2,
        parse_status="draft",
        extra={},
    )
    production = AgentProduction(
        id=uuid4(),
        project_id=agent_api.project.id,
        user_id=agent_api.owner.id,
        source_document_id=source.id,
        status="planning",
        current_stage="source_analysis",
        mode="supervised",
        production_spec={"text_model_id": str(agent_api.text_model.id)},
        estimated_points=0,
        consumed_points=0,
        lock_version=0,
        extra={},
    )
    step = AgentStep(
        id=uuid4(),
        production_id=production.id,
        stage="source_analysis",
        scope_type="production",
        scope_id=production.id,
        status="queued",
        input_version=1,
        progress_current=0,
        progress_total=1,
        attempt_count=0,
        extra={},
    )
    agent_api.session.add_all([source, production, step])
    await agent_api.session.commit()

    first = await advance_source_analysis(
        agent_api.session,
        production.id,
        step.id,
    )
    assert len(first.task_record_ids) == 1
    charged_balance = agent_api.owner.points_balance
    charged_points = production.consumed_points

    step.status = "queued"
    step.extra = {**(step.extra or {}), "redispatch_requested": True}
    await agent_api.session.commit()

    recovered = await advance_source_analysis(
        agent_api.session,
        production.id,
        step.id,
    )

    assert recovered.task_record_ids == first.task_record_ids
    assert production.consumed_points == charged_points == 7
    assert agent_api.owner.points_balance == charged_balance == 93


@pytest.mark.asyncio
async def test_workflow_v2_runs_exactly_two_model_tasks_in_sequence(
    agent_api,
) -> None:
    content = "雨夜，沈砚走进旧宅寻找玉佩。"
    source = ProjectSourceDocument(
        id=uuid4(),
        project_id=agent_api.project.id,
        user_id=agent_api.owner.id,
        source_type="text",
        content=content,
        content_hash="c" * 64,
        character_count=len(content),
        version=2,
        parse_status="draft",
        extra={},
    )
    production = AgentProduction(
        id=uuid4(),
        project_id=agent_api.project.id,
        user_id=agent_api.owner.id,
        source_document_id=source.id,
        status="planning",
        current_stage="source_analysis",
        mode="supervised",
        production_spec={
            "workflow_version": 2,
            "text_model_id": str(agent_api.text_model.id),
        },
        estimated_points=0,
        consumed_points=0,
        lock_version=0,
        extra={},
    )
    step = AgentStep(
        id=uuid4(),
        production_id=production.id,
        stage="source_analysis",
        scope_type="production",
        scope_id=production.id,
        status="queued",
        input_version=1,
        progress_current=0,
        progress_total=1,
        attempt_count=0,
        extra={},
    )
    agent_api.session.add_all([source, production, step])
    await agent_api.session.commit()

    plan_result = await advance_source_analysis(
        agent_api.session,
        production.id,
        step.id,
    )
    assert len(plan_result.task_record_ids) == 1
    plan_record = await agent_api.session.get(
        UserTaskRecord,
        plan_result.task_record_ids[0],
    )
    assert plan_record.generation_type == "agent_episode_planning"
    assert all("task_record_id" not in item for item in step.extra["chunks"])
    assert content in await build_agent_text_prompt(agent_api.session, plan_record)
    plan_record.status = "success"
    plan_record.extra = {
        **(plan_record.extra or {}),
        "parsed_result": {
            "planning_summary": "单集结构",
            "episodes": [
                {
                    "episode_number": 1,
                    "title": "雨夜寻物",
                    "content": content,
                    "opening_hook": "沈砚进入旧宅",
                    "goal": "寻找玉佩",
                    "conflict": "旧宅环境阻碍调查",
                    "climax": "发现玉佩",
                    "ending_hook": "玉佩来历成谜",
                    "source_start": 0,
                    "source_end": len(content),
                    "source_end_quote": content[-10:],
                    "continuity_notes": [],
                }
            ],
        },
    }
    await agent_api.session.commit()

    asset_result = await advance_source_analysis(
        agent_api.session,
        production.id,
        step.id,
    )
    assert len(asset_result.task_record_ids) == 1
    asset_record = await agent_api.session.get(
        UserTaskRecord,
        asset_result.task_record_ids[0],
    )
    assert asset_record.generation_type == "agent_asset_analysis"
    asset_prompt = await build_agent_text_prompt(agent_api.session, asset_record)
    assert content in asset_prompt
    assert "episode_number" in asset_prompt
    asset_record.status = "success"
    asset_record.extra = {
        **(asset_record.extra or {}),
        "parsed_result": {
            "characters": [
                {
                    "name": "沈砚",
                    "aliases": [],
                    "source_facts": {"story_role": "寻找玉佩的调查者"},
                    "design_spec": {
                        "apparent_age": "二十七八岁",
                        "identity_anchors": ["黑色短发", "高挑偏瘦"],
                        "default_costume": "黑色长风衣",
                    },
                    "source_evidence": [
                        {"source_start": 3, "source_end": 5, "source_quote": "沈砚"}
                    ],
                }
            ],
            "character_variants": [],
            "scenes": [],
            "scene_variants": [],
            "props": [],
            "prop_variants": [],
        },
    }
    await agent_api.session.commit()

    completed = await advance_source_analysis(
        agent_api.session,
        production.id,
        step.id,
    )
    assert completed.completed is True
    assert completed.task_record_ids == []

    records = (
        await agent_api.session.execute(
            select(UserTaskRecord)
            .where(
                UserTaskRecord.extra["agent_production_id"].as_string()
                == str(production.id)
            )
            .order_by(UserTaskRecord.created_at)
        )
    ).scalars().all()
    assert [record.generation_type for record in records] == [
        "agent_episode_planning",
        "agent_asset_analysis",
    ]
    assert step.extra["asset_analysis_task_record_id"]
    assert step.extra["episode_plan_task_record_id"]
    assert step.progress_total == 2


@pytest.mark.asyncio
async def test_agent_controller_lease_rejects_duplicates_and_recovers_after_expiry(agent_api) -> None:
    first_token = await queue_agent_controller_claim(agent_api.session, agent_api.production.id)
    assert first_token is not None
    assert await queue_agent_controller_claim(agent_api.session, agent_api.production.id) is None

    state_result = await agent_api.session.execute(
        select(AgentControllerState).where(
            AgentControllerState.production_id == agent_api.production.id
        )
    )
    state = state_result.scalar_one()
    state.lease_expires_at = beijing_datetime() - timedelta(seconds=1)
    await agent_api.session.commit()

    second_token = await queue_agent_controller_claim(agent_api.session, agent_api.production.id)
    assert second_token is not None
    assert second_token != first_token
    assert await start_agent_controller_claim(
        agent_api.session, agent_api.production.id, first_token
    ) is False
    assert await start_agent_controller_claim(
        agent_api.session, agent_api.production.id, second_token
    ) is True

    await fail_agent_controller_claim(
        agent_api.session,
        agent_api.production.id,
        second_token,
        "controller crashed",
    )
    await agent_api.session.refresh(state)
    assert state.status == "failed"
    assert state.attempt_count == 1
    assert state.last_error == "controller crashed"
    event_result = await agent_api.session.execute(
        select(AgentEvent).where(
            AgentEvent.production_id == agent_api.production.id,
            AgentEvent.event_type == "controller.failed",
        )
    )
    assert event_result.scalar_one().payload["error"] == "controller crashed"
    assert await queue_agent_controller_claim(agent_api.session, agent_api.production.id) is None

    state.lease_expires_at = beijing_datetime() - timedelta(seconds=1)
    await agent_api.session.commit()
    recovery_token = await queue_agent_controller_claim(agent_api.session, agent_api.production.id)
    assert recovery_token is not None
    assert await start_agent_controller_claim(
        agent_api.session, agent_api.production.id, recovery_token
    ) is True
    await finish_agent_controller_claim(
        agent_api.session,
        agent_api.production.id,
        recovery_token,
        advanced=False,
    )
    await agent_api.session.refresh(state)
    assert state.status == "idle"
    assert state.attempt_count == 2
    assert state.last_error is None
    assert state.last_result == {"advanced": False}


@pytest.mark.asyncio
async def test_core_asset_management_crud_search_and_variant_lifecycle(agent_api) -> None:
    story_step = AgentStep(
        id=uuid4(),
        production_id=agent_api.production.id,
        stage="story_bible",
        scope_type="production",
        scope_id=agent_api.production.id,
        status="completed",
        input_version=1,
        output_version=1,
        progress_current=1,
        progress_total=1,
        attempt_count=1,
        extra={},
    )
    bible = SeriesBibleVersion(
        id=uuid4(),
        project_id=agent_api.project.id,
        production_id=agent_api.production.id,
        step_id=story_step.id,
        version=1,
        status="confirmed",
        content={},
        created_by=agent_api.owner.id,
        confirmed_by=agent_api.owner.id,
    )
    agent_api.production.current_stage = "core_assets"
    agent_api.session.add(story_step)
    await agent_api.session.flush()
    agent_api.session.add(bible)
    await agent_api.session.commit()

    prefix = f"/api/v1/agent-productions/{agent_api.production.id}/core-assets"
    created = await agent_api.client.post(
        prefix,
        json={
            "asset_type": "character",
            "canonical_name": "沈砚",
            "aliases": ["阿砚"],
            "content": {
                "identity": "调查员",
                "appearance": "黑发灰眼",
                "costume": "灰色长风衣",
            },
        },
    )
    assert created.status_code == 200
    created_data = created.json()["data"]
    asset_id = created_data["asset_id"]
    assert created_data["lock_version"] == 0
    assert created_data["content"]["identity"] == "调查员"

    searched = await agent_api.client.get(f"{prefix}?keyword=灰眼&asset_type=character")
    assert searched.status_code == 200
    assert searched.json()["data"]["total"] == 1
    assert searched.json()["data"]["items"][0]["asset_id"] == asset_id

    updated = await agent_api.client.patch(
        f"{prefix}/character/{asset_id}",
        json={
            "expected_lock_version": 0,
            "canonical_name": "沈砚（成年）",
            "content": {"appearance": "黑发金瞳"},
        },
    )
    assert updated.status_code == 200
    assert updated.json()["data"]["lock_version"] == 1
    assert updated.json()["data"]["content"]["identity"] == "调查员"
    assert updated.json()["data"]["content"]["appearance"] == "黑发金瞳"

    variant = await agent_api.client.post(
        f"{prefix}/character/{asset_id}/variants",
        json={
            "canonical_name": "沈砚夜行装",
            "variant_type": "costume",
            "description": "黑色夜行服",
            "trigger_reason": "潜入旧宅",
            "episode_numbers": [1],
            "source_evidence": [
                {
                    "source_start": 0,
                    "source_end": 2,
                    "source_quote": "沈砚",
                }
            ],
        },
    )
    assert variant.status_code == 200
    variant_id = variant.json()["data"]["id"]

    detail = await agent_api.client.get(f"{prefix}/character/{asset_id}")
    assert detail.status_code == 200
    assert detail.json()["data"]["lock_version"] == 2
    assert [item["id"] for item in detail.json()["data"]["variants"]] == [variant_id]

    deleted_variant = await agent_api.client.delete(
        f"{prefix}/character/{asset_id}/variants/{variant_id}?expected_lock_version=0"
    )
    assert deleted_variant.status_code == 200
    assert deleted_variant.json()["data"]["deleted"] is True

    replacement_variant = await agent_api.client.post(
        f"{prefix}/character/{asset_id}/variants",
        json={
            "canonical_name": "沈砚伪装",
            "variant_type": "disguise",
            "description": "戴帽子的临时伪装",
            "trigger_reason": "躲避追踪",
        },
    )
    assert replacement_variant.status_code == 200

    detail = await agent_api.client.get(f"{prefix}/character/{asset_id}")
    current_version = detail.json()["data"]["lock_version"]
    deleted = await agent_api.client.delete(
        f"{prefix}/character/{asset_id}?expected_lock_version={current_version}"
    )
    assert deleted.status_code == 200
    assert deleted.json()["data"]["deleted_variant_count"] == 1

    empty = await agent_api.client.get(prefix)
    assert empty.status_code == 200
    assert empty.json()["data"]["total"] == 0
    readiness = await agent_api.client.get(f"{prefix}/readiness")
    assert readiness.status_code == 200
    assert readiness.json()["data"]["items"] == []
    assert readiness.json()["data"]["can_lock"] is False


@pytest.mark.asyncio
async def test_core_asset_lock_flow_generates_previews_and_invalidates_by_stable_id(
    agent_api,
    monkeypatch,
) -> None:
    story_step = AgentStep(
        id=uuid4(),
        production_id=agent_api.production.id,
        stage="story_bible",
        scope_type="production",
        scope_id=agent_api.production.id,
        status="completed",
        input_version=1,
        output_version=1,
        progress_current=1,
        progress_total=1,
        attempt_count=1,
        extra={},
    )
    bible = SeriesBibleVersion(
        id=uuid4(),
        project_id=agent_api.project.id,
        production_id=agent_api.production.id,
        step_id=story_step.id,
        version=1,
        status="confirmed",
        content={},
        created_by=agent_api.owner.id,
        confirmed_by=agent_api.owner.id,
    )
    character = ProjectCharacter(
        id=uuid4(),
        project_id=agent_api.project.id,
        user_id=agent_api.owner.id,
        name="沈砚",
        aliases=["阿砚"],
        reference_image=None,
        extra={},
        is_enabled=True,
    )
    scene = ProjectScene(
        id=uuid4(),
        project_id=agent_api.project.id,
        user_id=agent_api.owner.id,
        name="旧宅",
        reference_image="https://example.com/house-v1.png",
        extra={},
        is_enabled=True,
    )
    character_candidate = AgentAssetCandidate(
        id=uuid4(),
        project_id=agent_api.project.id,
        production_id=agent_api.production.id,
        bible_version_id=bible.id,
        user_id=agent_api.owner.id,
        asset_type="character",
        candidate_key="c" * 64,
        canonical_name=character.name,
        aliases=character.aliases,
        source_chapter_ids=[str(agent_api.chapter.id)],
        confidence=1,
        merge_reason="标准名称一致",
        review_status="materialized",
        content={},
        materialized_asset_id=character.id,
        lock_version=1,
    )
    scene_candidate = AgentAssetCandidate(
        id=uuid4(),
        project_id=agent_api.project.id,
        production_id=agent_api.production.id,
        bible_version_id=bible.id,
        user_id=agent_api.owner.id,
        asset_type="scene",
        candidate_key="s" * 64,
        canonical_name=scene.name,
        aliases=[],
        source_chapter_ids=[str(agent_api.chapter.id)],
        confidence=1,
        merge_reason="标准名称一致",
        review_status="materialized",
        content={},
        materialized_asset_id=scene.id,
        lock_version=1,
    )
    character_variant = AgentAssetVariant(
        id=uuid4(),
        project_id=agent_api.project.id,
        production_id=agent_api.production.id,
        bible_version_id=bible.id,
        base_candidate_id=character_candidate.id,
        user_id=agent_api.owner.id,
        asset_type="character",
        variant_key="v" * 64,
        canonical_name="沈砚受伤造型",
        variant_type="injury",
        description="额角伤口与血迹",
        trigger_reason="遭到袭击",
        episode_numbers=[1],
        source_evidence=[],
        confidence=1,
        review_status="ready",
        content={
            "visual_delta": {"add": ["额角伤口"], "replace": [], "remove": []},
            "preserve_anchors": ["黑色短发", "窄长脸"],
        },
        extra={},
        lock_version=0,
    )
    storyboard = ProjectStoryboard(
        id=uuid4(),
        project_id=agent_api.project.id,
        chapter_id=agent_api.chapter.id,
        user_id=agent_api.owner.id,
        shot_number=1,
        title="旧宅对峙",
        source_content="沈砚进入旧宅。",
        characters=["沈砚"],
        props=[],
        extra={
            "image_reference_asset_ids": {
                "character": [str(character.id)],
                "scene": [str(scene.id)],
                "prop": [],
            },
            "image_generation_status": "success",
            "video_generation_status": "success",
            "image_generation_result": "https://example.com/shot.png",
        },
        is_enabled=True,
    )
    image_history = ProjectGeneratedAsset(
        id=uuid4(),
        project_id=agent_api.project.id,
        chapter_id=agent_api.chapter.id,
        user_id=agent_api.owner.id,
        target_type="storyboard",
        target_id=storyboard.id,
        media_type="image",
        result_url="https://example.com/shot.png",
        result_urls=["https://example.com/shot.png"],
        status="success",
        is_selected=True,
        extra={},
        is_enabled=True,
    )
    video_history = ProjectGeneratedAsset(
        id=uuid4(),
        project_id=agent_api.project.id,
        chapter_id=agent_api.chapter.id,
        user_id=agent_api.owner.id,
        target_type="storyboard",
        target_id=storyboard.id,
        media_type="video",
        result_url="https://example.com/shot.mp4",
        result_urls=["https://example.com/shot.mp4"],
        status="success",
        is_selected=True,
        extra={},
        is_enabled=True,
    )
    agent_api.production.current_stage = "core_assets"
    agent_api.session.add_all(
        [
            story_step,
            character,
            scene,
            storyboard,
        ]
    )
    await agent_api.session.flush()
    agent_api.session.add(bible)
    await agent_api.session.flush()
    agent_api.session.add_all(
        [
            character_candidate,
            scene_candidate,
            character_variant,
            image_history,
            video_history,
        ]
    )
    await agent_api.session.commit()

    monkeypatch.setattr(
        "app.tasks.project_asset_generation.run_project_asset_image_generation.apply_async",
        lambda **_kwargs: None,
    )
    prefix = f"/api/v1/agent-productions/{agent_api.production.id}/core-assets"
    readiness = await agent_api.client.get(f"{prefix}/readiness")
    assert readiness.status_code == 200
    assert readiness.json()["data"]["can_lock"] is True

    variant_uploaded = await agent_api.client.put(
        f"{prefix}/character/{character.id}/variants/{character_variant.id}/reference-image",
        json={
            "expected_lock_version": character_variant.lock_version,
            "reference_image": "https://example.com/uploaded-variant.png",
        },
    )
    assert variant_uploaded.status_code == 200
    assert variant_uploaded.json()["data"]["reference_image"] == (
        "https://example.com/uploaded-variant.png"
    )

    blocked_variant = await agent_api.client.post(
        f"{prefix}/character/{character.id}/variants/{character_variant.id}/image-generation",
        json={},
    )
    assert blocked_variant.status_code == 409
    assert blocked_variant.json()["code"] == 40984

    base_uploaded = await agent_api.client.put(
        f"{prefix}/character/{character.id}/reference-image",
        json={
            "expected_lock_version": character_candidate.lock_version,
            "reference_image": "https://example.com/shenyan-v1.png",
        },
    )
    assert base_uploaded.status_code == 200

    variant_generation = await agent_api.client.post(
        f"{prefix}/character/{character.id}/variants/{character_variant.id}/image-generation",
        json={"prompt": "保留人物身份，只增加额角伤口"},
    )
    assert variant_generation.status_code == 200
    variant_task = await agent_api.session.get(
        UserTaskRecord,
        UUID(variant_generation.json()["data"]["task_record_id"]),
    )
    assert variant_task.extra["model_extra"]["images"] == [
        "https://example.com/shenyan-v1.png"
    ]

    async def fake_variant_model(*_args, **_kwargs):
        return ModelRunResult(
            content="https://example.com/generated-variant.png",
            extra={},
        )

    async def keep_generated_variant(_media_type, result):
        return result

    monkeypatch.setattr(
        "app.services.project_asset_generation.run_model",
        fake_variant_model,
    )
    monkeypatch.setattr(
        "app.services.project_asset_generation.persist_generated_media_to_oss",
        keep_generated_variant,
    )
    await run_asset_image_generation_in_worker(
        agent_api.session,
        variant_task,
        "character",
        character.id,
    )
    await agent_api.session.commit()
    await agent_api.session.refresh(character)
    await agent_api.session.refresh(character_variant)
    assert character.reference_image == "https://example.com/shenyan-v1.png"
    assert character_variant.reference_image == "https://example.com/generated-variant.png"

    uploaded = await agent_api.client.put(
        f"{prefix}/scene/{scene.id}/reference-image",
        json={
            "expected_lock_version": scene_candidate.lock_version,
            "reference_image": "https://example.com/uploaded-house.png",
        },
    )
    assert uploaded.status_code == 200
    assert uploaded.json()["data"]["reference_image"] == (
        "https://example.com/uploaded-house.png"
    )
    assert uploaded.json()["data"]["image_generation_status"] == "selected"

    batch = await agent_api.client.post(
        f"{prefix}/image-generations",
        json={
            "ai_model_id": str(agent_api.image_model.id),
            "items": [
                {
                    "asset_type": "character",
                    "asset_id": str(character.id),
                    "prompt": "保持灰色长风衣",
                }
            ],
        },
    )
    assert batch.status_code == 200
    assert batch.json()["data"]["submitted_count"] == 1
    assert batch.json()["data"]["total_points_cost"] == 3

    selection = {
        "expected_lock_version": 0,
        "assets": [
            {"asset_type": "character", "asset_id": str(character.id)},
            {"asset_type": "scene", "asset_id": str(scene.id)},
        ],
    }
    initial_impact = await agent_api.client.post(
        f"{prefix}/impact-preview",
        json=selection,
    )
    assert initial_impact.status_code == 200
    assert initial_impact.json()["data"]["affected_storyboard_count"] == 0

    first_lock = await agent_api.client.post(
        f"{prefix}/confirm",
        json={
            "expected_lock_version": 0,
            "idempotency_key": "core-lock-v1",
        },
    )
    assert first_lock.status_code == 200
    assert first_lock.json()["data"]["version"] == 1
    assert {
        (item["asset_type"], item["asset_id"])
        for item in first_lock.json()["data"]["assets"]
    } == {
        ("character", str(character.id)),
        ("scene", str(scene.id)),
    }
    character_snapshot = next(
        item
        for item in first_lock.json()["data"]["assets"]
        if item["asset_type"] == "character"
    )
    assert character_snapshot["variants"][0]["variant_id"] == str(character_variant.id)
    assert character_snapshot["variants"][0]["reference_image"] == (
        "https://example.com/generated-variant.png"
    )
    await agent_api.session.refresh(character)
    character.extra = {
        **(character.extra or {}),
        "image_generation_status": "success",
    }
    await agent_api.session.commit()

    async def fake_post_confirm_variant_model(*_args, **_kwargs):
        return ModelRunResult(
            content="https://example.com/generated-variant-v2.png",
            extra={},
        )

    monkeypatch.setattr(
        "app.services.project_asset_generation.run_model",
        fake_post_confirm_variant_model,
    )
    post_confirm_generation = await agent_api.client.post(
        f"{prefix}/character/{character.id}/variants/{character_variant.id}/image-generation",
        json={"prompt": "确认后重新生成受伤造型"},
    )
    assert post_confirm_generation.status_code == 200
    post_confirm_task = await agent_api.session.get(
        UserTaskRecord,
        UUID(post_confirm_generation.json()["data"]["task_record_id"]),
    )
    await run_asset_image_generation_in_worker(
        agent_api.session,
        post_confirm_task,
        "character",
        character.id,
    )
    await agent_api.session.commit()
    generated_change_review = await agent_api.client.get(
        f"/api/v1/agent-productions/{agent_api.production.id}"
    )
    assert generated_change_review.json()["data"]["current_stage"] == (
        "core_asset_change_review"
    )

    await agent_api.session.refresh(character_variant)
    variant_reverted = await agent_api.client.put(
        f"{prefix}/character/{character.id}/variants/{character_variant.id}/reference-image",
        json={
            "expected_lock_version": character_variant.lock_version,
            "reference_image": "https://example.com/generated-variant.png",
        },
    )
    assert variant_reverted.status_code == 200
    resumed_after_variant_revert = await agent_api.client.get(
        f"/api/v1/agent-productions/{agent_api.production.id}"
    )
    assert resumed_after_variant_revert.json()["data"]["current_stage"] == (
        "pilot_production"
    )

    character_detail = await agent_api.client.get(f"{prefix}/character/{character.id}")
    changed = await agent_api.client.put(
        f"{prefix}/character/{character.id}/reference-image",
        json={
            "expected_lock_version": character_detail.json()["data"]["lock_version"],
            "reference_image": "https://example.com/shenyan-v2.png",
        },
    )
    assert changed.status_code == 200
    change_review = await agent_api.client.get(
        f"/api/v1/agent-productions/{agent_api.production.id}"
    )
    assert change_review.json()["data"]["current_stage"] == "core_asset_change_review"

    reverted = await agent_api.client.put(
        f"{prefix}/character/{character.id}/reference-image",
        json={
            "expected_lock_version": changed.json()["data"]["lock_version"],
            "reference_image": "https://example.com/shenyan-v1.png",
        },
    )
    assert reverted.status_code == 200
    resumed_without_relock = await agent_api.client.get(
        f"/api/v1/agent-productions/{agent_api.production.id}"
    )
    assert resumed_without_relock.json()["data"]["current_stage"] == "pilot_production"

    changed_again = await agent_api.client.put(
        f"{prefix}/character/{character.id}/reference-image",
        json={
            "expected_lock_version": reverted.json()["data"]["lock_version"],
            "reference_image": "https://example.com/shenyan-v2.png",
        },
    )
    assert changed_again.status_code == 200
    change_review_again = await agent_api.client.get(
        f"/api/v1/agent-productions/{agent_api.production.id}"
    )
    assert change_review_again.json()["data"]["current_stage"] == "core_asset_change_review"

    changed_selection = {**selection, "expected_lock_version": 1}
    impact = await agent_api.client.post(
        f"{prefix}/impact-preview",
        json=changed_selection,
    )
    impact_data = impact.json()["data"]
    assert impact_data["affected_storyboard_count"] == 1
    assert impact_data["affected_image_count"] == 1
    assert impact_data["affected_video_count"] == 1
    assert impact_data["changes"][0]["change_type"] == "reference_changed"

    stale = await agent_api.client.post(
        f"{prefix}/lock",
        json={**changed_selection, "idempotency_key": "core-lock-v2"},
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == 40935

    second_lock = await agent_api.client.post(
        f"{prefix}/confirm",
        json={
            "expected_lock_version": 1,
            "idempotency_key": "core-lock-v2",
        },
    )
    assert second_lock.status_code == 200
    assert second_lock.json()["data"]["version"] == 2
    resumed = await agent_api.client.get(f"/api/v1/agent-productions/{agent_api.production.id}")
    assert resumed.json()["data"]["current_stage"] == "pilot_production"

    storyboard_detail = await agent_api.client.get(
        f"/api/v1/projects/{agent_api.project.id}/chapters/{agent_api.chapter.id}"
        f"/storyboards/{storyboard.id}"
    )
    assert storyboard_detail.json()["data"]["extra"]["image_generation_status"] == "invalidated"
    assert storyboard_detail.json()["data"]["extra"]["video_generation_status"] == "invalidated"


@pytest.mark.asyncio
async def test_pilot_production_runs_storyboard_image_video_and_confirmation_flow(
    agent_api,
    monkeypatch,
) -> None:
    story_step = AgentStep(
        id=uuid4(),
        production_id=agent_api.production.id,
        stage="story_bible",
        scope_type="production",
        scope_id=agent_api.production.id,
        status="completed",
        input_version=1,
        output_version=1,
        progress_current=1,
        progress_total=1,
        attempt_count=1,
        extra={},
    )
    core_step = AgentStep(
        id=uuid4(),
        production_id=agent_api.production.id,
        stage="core_assets",
        scope_type="production",
        scope_id=agent_api.production.id,
        status="completed",
        input_version=1,
        output_version=1,
        progress_current=2,
        progress_total=2,
        attempt_count=1,
        extra={},
    )
    bible = SeriesBibleVersion(
        id=uuid4(),
        project_id=agent_api.project.id,
        production_id=agent_api.production.id,
        step_id=story_step.id,
        version=1,
        status="confirmed",
        content={},
        created_by=agent_api.owner.id,
        confirmed_by=agent_api.owner.id,
    )
    character = ProjectCharacter(
        id=uuid4(),
        project_id=agent_api.project.id,
        user_id=agent_api.owner.id,
        name="沈砚",
        aliases=["阿砚"],
        reference_image="https://example.com/shenyan.png",
        extra={},
        is_enabled=True,
    )
    scene = ProjectScene(
        id=uuid4(),
        project_id=agent_api.project.id,
        user_id=agent_api.owner.id,
        name="旧宅",
        reference_image="https://example.com/house.png",
        extra={},
        is_enabled=True,
    )
    batch_chapter = ProjectChapter(
        id=uuid4(),
        project_id=agent_api.project.id,
        user_id=agent_api.owner.id,
        title="第二集",
        content="沈砚带着玉佩离开旧宅。",
        processed_content="沈砚带着玉佩离开旧宅。",
        process_status="completed",
        sort_order=2,
        extra={
            "agent_production_id": str(agent_api.production.id),
            "episode_number": 2,
        },
        is_enabled=True,
    )
    agent_api.production.current_stage = "pilot_production"
    agent_api.production.production_spec = {
        **(agent_api.production.production_spec or {}),
        "generate_audio": True,
    }
    agent_api.owner.points_balance = 1000
    agent_api.session.add_all([story_step, core_step, character, scene, batch_chapter])
    await agent_api.session.flush()
    agent_api.session.add(bible)
    await agent_api.session.flush()
    core_lock = AgentCoreAssetLock(
        id=uuid4(),
        project_id=agent_api.project.id,
        production_id=agent_api.production.id,
        bible_version_id=bible.id,
        step_id=core_step.id,
        version=1,
        status="active",
        assets=[
            {
                "asset_type": "character",
                "asset_id": str(character.id),
                "name": character.name,
                "reference_image": character.reference_image,
            },
            {
                "asset_type": "scene",
                "asset_id": str(scene.id),
                "name": scene.name,
                "reference_image": scene.reference_image,
            },
        ],
        impact={},
        idempotency_key="pilot-core-lock-v1",
        created_by=agent_api.owner.id,
    )
    agent_api.session.add(core_lock)
    await agent_api.session.commit()

    monkeypatch.setattr(
        "app.tasks.project_storyboard.run_project_storyboard_analysis.apply_async",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        "app.tasks.project_storyboard_image.run_project_storyboard_image_generation.apply_async",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        "app.tasks.project_storyboard_video.run_project_storyboard_video_generation.apply_async",
        lambda **_kwargs: None,
    )
    prefix = f"/api/v1/agent-productions/{agent_api.production.id}/pilot"
    action = {
        "expected_core_asset_lock_version": 1,
        "idempotency_key": "pilot-storyboards-v1",
    }
    started = await agent_api.client.post(f"{prefix}/storyboards", json=action)
    assert started.status_code == 200
    assert started.json()["data"]["phase"] == "storyboards"
    assert started.json()["data"]["submitted_points"] == 7

    await agent_api.session.refresh(agent_api.chapter)
    analysis_task_id = UUID(agent_api.chapter.extra["storyboard_analysis_task_record_id"])
    analysis_task = await agent_api.session.get(UserTaskRecord, analysis_task_id)
    analysis_task.status = "success"
    agent_api.chapter.extra = {
        **(agent_api.chapter.extra or {}),
        "storyboard_analysis_status": "success",
    }
    storyboard = ProjectStoryboard(
        id=uuid4(),
        project_id=agent_api.project.id,
        chapter_id=agent_api.chapter.id,
        user_id=agent_api.owner.id,
        shot_number=1,
        title="沈砚进入旧宅",
        source_content="沈砚在旧宅发现玉佩。",
        scene_name="旧宅",
        characters=["阿砚"],
        props=[],
        action="沈砚进入旧宅并观察四周。",
        extra={"task_record_id": str(analysis_task.id)},
        is_enabled=True,
    )
    agent_api.session.add(storyboard)
    await agent_api.session.commit()

    review = await agent_api.client.get(prefix)
    assert review.status_code == 200
    assert review.json()["data"]["phase"] == "storyboard_review"
    assert review.json()["data"]["can_submit_images"] is True
    await agent_api.session.refresh(storyboard)
    assert storyboard.extra["agent_asset_ids"]["character"] == [str(character.id)]
    assert storyboard.extra["agent_asset_ids"]["scene"] == [str(scene.id)]

    storyboard_prefix = (
        f"/api/v1/projects/{agent_api.project.id}/chapters/{agent_api.chapter.id}"
        f"/storyboards/{storyboard.id}"
    )
    unbound = await agent_api.client.patch(
        storyboard_prefix,
        json={"characters": ["陌生人"]},
    )
    assert unbound.status_code == 200
    blocked_review = await agent_api.client.get(prefix)
    assert blocked_review.json()["data"]["error_count"] == 1
    assert blocked_review.json()["data"]["can_submit_images"] is False
    blocked_images = await agent_api.client.post(
        f"{prefix}/images",
        json={**action, "idempotency_key": "pilot-images-blocked-v1"},
    )
    assert blocked_images.status_code == 409
    assert blocked_images.json()["code"] == 40942
    rebound = await agent_api.client.patch(
        storyboard_prefix,
        json={"characters": ["阿砚"]},
    )
    assert rebound.status_code == 200
    rebound_review = await agent_api.client.get(prefix)
    assert rebound_review.json()["data"]["error_count"] == 0

    images = await agent_api.client.post(
        f"{prefix}/images",
        json={**action, "idempotency_key": "pilot-images-v1"},
    )
    assert images.status_code == 200
    assert images.json()["data"]["phase"] == "images"
    assert images.json()["data"]["image_status_counts"]["pending"] == 1
    await agent_api.session.refresh(storyboard)
    image_task = await agent_api.session.get(
        UserTaskRecord,
        UUID(storyboard.extra["image_generation_task_record_id"]),
    )
    image_task.status = "success"
    storyboard.extra = {
        **(storyboard.extra or {}),
        "image_generation_status": "success",
        "image_generation_result": "https://example.com/pilot-shot.png",
    }
    await agent_api.session.commit()

    images_ready = await agent_api.client.get(prefix)
    assert images_ready.json()["data"]["phase"] == "images_ready"
    assert images_ready.json()["data"]["can_submit_videos"] is True

    edited_after_image = await agent_api.client.patch(
        storyboard_prefix,
        json={"action": "沈砚推开旧宅木门并警惕观察。"},
    )
    assert edited_after_image.status_code == 200
    regressed = await agent_api.client.get(prefix)
    assert regressed.json()["data"]["phase"] == "images"
    assert regressed.json()["data"]["image_status_counts"]["invalidated"] == 1
    await agent_api.session.refresh(storyboard)
    storyboard.extra = {
        **(storyboard.extra or {}),
        "image_generation_status": "success",
        "image_generation_result": "https://example.com/pilot-shot-v2.png",
    }
    await agent_api.session.commit()
    images_ready_again = await agent_api.client.get(prefix)
    assert images_ready_again.json()["data"]["phase"] == "images_ready"

    videos = await agent_api.client.post(
        f"{prefix}/videos",
        json={**action, "idempotency_key": "pilot-videos-v1"},
    )
    assert videos.status_code == 200
    assert videos.json()["data"]["phase"] == "videos"
    assert videos.json()["data"]["video_status_counts"]["pending"] == 1
    await agent_api.session.refresh(storyboard)
    video_task = await agent_api.session.get(
        UserTaskRecord,
        UUID(storyboard.extra["video_generation_task_record_id"]),
    )
    assert video_task.extra["agent_production_id"] == str(agent_api.production.id)
    assert video_task.extra["agent_stage"] == "pilot_videos"
    assert video_task.extra["model_extra"]["generate_audio"] is True
    video_task.status = "success"
    storyboard.extra = {
        **(storyboard.extra or {}),
        "video_generation_status": "success",
        "video_generation_result": "https://example.com/pilot-shot.mp4",
    }
    await agent_api.session.commit()

    final_review = await agent_api.client.get(prefix)
    assert final_review.json()["data"]["phase"] == "final_review"
    assert final_review.json()["data"]["can_confirm"] is True
    assert final_review.json()["data"]["final_checkpoint_id"]

    confirmed = await agent_api.client.post(
        f"{prefix}/confirm",
        json={**action, "idempotency_key": "pilot-confirm-v1"},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["data"]["phase"] == "completed"
    assert confirmed.json()["data"]["current_stage"] == "batch_production"

    batch_prefix = f"/api/v1/agent-productions/{agent_api.production.id}/batch"
    batch_action = {
        "expected_core_asset_lock_version": 1,
        "idempotency_key": "batch-storyboards-v1",
        "max_tasks": 5,
    }
    controller_candidates = await list_agent_controller_candidates(agent_api.session)
    assert agent_api.production.id not in controller_candidates
    agent_api.production.mode = "automatic"
    await agent_api.session.commit()
    controller_candidates = await list_agent_controller_candidates(agent_api.session)
    assert agent_api.production.id in controller_candidates
    assert await advance_agent_batch_production(agent_api.session, agent_api.production.id) is True
    batch_started = await agent_api.client.get(batch_prefix)
    assert batch_started.status_code == 200
    assert batch_started.json()["data"]["phase"] == "storyboards"
    assert batch_started.json()["data"]["episodes"][0]["storyboard_analysis_status"] == "pending"

    repeated = await agent_api.client.post(
        f"{batch_prefix}/dispatch",
        json=batch_action,
    )
    assert repeated.status_code == 200
    await agent_api.session.refresh(batch_chapter)
    batch_analysis_task_id = UUID(batch_chapter.extra["storyboard_analysis_task_record_id"])
    batch_analysis_tasks = await agent_api.session.get(UserTaskRecord, batch_analysis_task_id)
    assert batch_analysis_tasks.extra["agent_production_id"] == str(agent_api.production.id)
    assert batch_analysis_tasks.extra["agent_stage"] == "batch_storyboards"
    batch_analysis_tasks.status = "success"
    batch_chapter.extra = {
        **(batch_chapter.extra or {}),
        "storyboard_analysis_status": "success",
    }
    batch_storyboard = ProjectStoryboard(
        id=uuid4(),
        project_id=agent_api.project.id,
        chapter_id=batch_chapter.id,
        user_id=agent_api.owner.id,
        shot_number=1,
        title="携玉离宅",
        source_content="沈砚带着玉佩离开旧宅。",
        scene_name="旧宅",
        characters=["沈砚"],
        props=[],
        action="沈砚握住玉佩，快步离开旧宅。",
        extra={
            "task_record_id": str(batch_analysis_task_id),
            "estimated_duration_seconds": 11,
        },
        is_enabled=True,
    )
    agent_api.session.add(batch_storyboard)
    await agent_api.session.commit()

    images_ready_to_dispatch = await agent_api.client.get(batch_prefix)
    assert images_ready_to_dispatch.json()["data"]["phase"] == "images"
    images_dispatched = await agent_api.client.post(
        f"{batch_prefix}/dispatch",
        json={**batch_action, "idempotency_key": "batch-images-v1"},
    )
    assert images_dispatched.status_code == 200
    assert images_dispatched.json()["data"]["image_status_counts"]["pending"] == 1

    paused = await agent_api.client.post(
        f"/api/v1/agent-productions/{agent_api.production.id}/pause"
    )
    assert paused.status_code == 200
    assert paused.json()["data"]["status"] == "paused"

    await agent_api.session.refresh(batch_storyboard)
    batch_image_task = await agent_api.session.get(
        UserTaskRecord,
        UUID(batch_storyboard.extra["image_generation_task_record_id"]),
    )
    assert batch_image_task.extra["agent_production_id"] == str(agent_api.production.id)
    assert batch_image_task.extra["agent_stage"] == "batch_images"
    batch_image_task.status = "success"
    batch_storyboard.extra = {
        **(batch_storyboard.extra or {}),
        "image_generation_status": "success",
        "image_generation_result": "https://example.com/batch-shot.png",
    }
    await agent_api.session.commit()

    paused_status = await agent_api.client.get(batch_prefix)
    assert paused_status.json()["data"]["phase"] == "videos"
    assert paused_status.json()["data"]["paused"] is True
    assert paused_status.json()["data"]["video_status_counts"]["not_started"] == 1

    resumed = await agent_api.client.post(
        f"/api/v1/agent-productions/{agent_api.production.id}/resume"
    )
    assert resumed.status_code == 200
    await agent_api.session.refresh(batch_storyboard)
    assert batch_storyboard.extra["video_generation_status"] == "pending"

    batch_video_task = await agent_api.session.get(
        UserTaskRecord,
        UUID(batch_storyboard.extra["video_generation_task_record_id"]),
    )
    assert batch_video_task.extra["agent_production_id"] == str(agent_api.production.id)
    assert batch_video_task.extra["agent_stage"] == "batch_videos"
    assert batch_video_task.extra["model_extra"]["duration_seconds"] == 11
    assert batch_video_task.extra["model_extra"]["generate_audio"] is True
    batch_video_task.status = "failed"
    batch_video_task.result = "内容审核失败"
    batch_video_task.extra = {
        **(batch_video_task.extra or {}),
        "failed_reason": "内容审核失败",
    }
    batch_storyboard.extra = {
        **(batch_storyboard.extra or {}),
        "video_generation_status": "failed",
        "video_generation_failed_reason": "内容审核失败",
    }
    await agent_api.session.commit()

    failed_batch = await agent_api.client.get(batch_prefix)
    assert failed_batch.json()["data"]["phase"] == "exceptions"
    assert failed_batch.json()["data"]["status"] == "partially_failed"

    production_prefix = f"/api/v1/agent-productions/{agent_api.production.id}"
    matrix = await agent_api.client.get(f"{production_prefix}/matrix")
    assert matrix.status_code == 200
    batch_video_state = matrix.json()["data"]["episodes"][1]["storyboards"][0]["video"]
    assert batch_video_state["status"] == "failed"
    assert batch_video_state["can_retry"] is True

    exceptions = await agent_api.client.get(f"{production_prefix}/exceptions")
    assert exceptions.status_code == 200
    assert exceptions.json()["data"]["total"] == 1
    assert exceptions.json()["data"]["items"][0]["category"] == "content_moderation"

    retry_payload = {
        "expected_core_asset_lock_version": 1,
        "stage": "video",
        "scope_ids": [str(batch_storyboard.id)],
        "idempotency_key": "manual-retry-video-v1",
    }
    retry_without_confirmation = await agent_api.client.post(
        f"{production_prefix}/jobs/retry",
        json=retry_payload,
    )
    assert retry_without_confirmation.status_code == 409
    assert retry_without_confirmation.json()["code"] == 40964

    retried = await agent_api.client.post(
        f"{production_prefix}/jobs/retry",
        json={**retry_payload, "confirm_over_retry_limit": True},
    )
    repeated_retry = await agent_api.client.post(
        f"{production_prefix}/jobs/retry",
        json={**retry_payload, "confirm_over_retry_limit": True},
    )
    assert retried.status_code == 200
    assert retried.json()["data"]["affected_count"] == 1
    assert repeated_retry.json()["data"]["idempotent"] is True
    assert (
        repeated_retry.json()["data"]["task_record_ids"]
        == retried.json()["data"]["task_record_ids"]
    )

    await agent_api.session.refresh(batch_storyboard)
    retry_task = await agent_api.session.get(
        UserTaskRecord,
        UUID(batch_storyboard.extra["video_generation_task_record_id"]),
    )
    retry_task.status = "failed"
    retry_task.result = "内容审核失败"
    batch_storyboard.extra = {
        **(batch_storyboard.extra or {}),
        "video_generation_status": "failed",
        "video_generation_failed_reason": "内容审核失败",
    }
    await agent_api.session.commit()
    failed_again = await agent_api.client.get(batch_prefix)
    assert failed_again.json()["data"]["status"] == "partially_failed"

    skip_payload = {
        "expected_core_asset_lock_version": 1,
        "stage": "video",
        "scope_ids": [str(batch_storyboard.id)],
        "reason": "使用人工审核通过的视频替换",
        "replacement_url": "https://example.com/manual-batch-shot.mp4",
        "idempotency_key": "manual-replace-video-v1",
    }
    skipped = await agent_api.client.post(
        f"{production_prefix}/jobs/skip",
        json=skip_payload,
    )
    repeated_skip = await agent_api.client.post(
        f"{production_prefix}/jobs/skip",
        json=skip_payload,
    )
    assert skipped.status_code == 200
    assert skipped.json()["data"]["affected_count"] == 1
    assert repeated_skip.status_code == 200
    assert repeated_skip.json()["data"]["idempotent"] is True

    completed = await agent_api.client.get(batch_prefix)
    assert completed.status_code == 200
    assert completed.json()["data"]["phase"] == "completed"
    assert completed.json()["data"]["status"] == "completed"
    assert completed.json()["data"]["is_complete"] is True

    completed_matrix = await agent_api.client.get(f"{production_prefix}/matrix")
    resolved_video = completed_matrix.json()["data"]["episodes"][1]["storyboards"][0]["video"]
    assert resolved_video["status"] == "selected"
    assert resolved_video["manually_resolved"] is True
    assert resolved_video["replacement_url"] == skip_payload["replacement_url"]

    manual_history = await agent_api.client.get(
        f"/api/v1/projects/{agent_api.project.id}/chapters/{batch_chapter.id}"
        f"/storyboards/{batch_storyboard.id}/generation-history?media_type=video"
    )
    assert manual_history.status_code == 200
    assert (
        manual_history.json()["data"]["items"][0]["result_url"] == skip_payload["replacement_url"]
    )
    assert manual_history.json()["data"]["items"][0]["extra"]["manual_replacement"] is True

    costs = await agent_api.client.get(f"{production_prefix}/costs")
    assert costs.status_code == 200
    assert costs.json()["data"]["task_count"] >= 7
    assert any(item["stage"] == "video" for item in costs.json()["data"]["stages"])
    agent_task_count = costs.json()["data"]["task_count"]

    agent_api.session.add(
        UserTaskRecord(
            user_id=agent_api.owner.id,
            ai_model_id=agent_api.video_model.id,
            business_type="project",
            business_id=agent_api.project.id,
            generation_type="storyboard_video",
            status="success",
            title="同章节手工生成的视频",
            prompt="manual",
            points_cost=99,
            extra={
                "project_id": str(agent_api.project.id),
                "chapter_id": str(batch_chapter.id),
                "storyboard_id": str(batch_storyboard.id),
            },
        )
    )
    await agent_api.session.commit()
    costs_after_manual_task = await agent_api.client.get(f"{production_prefix}/costs")
    assert costs_after_manual_task.json()["data"]["task_count"] == agent_task_count
    assert costs_after_manual_task.json()["data"]["net_points"] == costs.json()["data"]["net_points"]

    events = await agent_api.client.get(f"{production_prefix}/events")
    assert events.status_code == 200
    event_types = {item["event_type"] for item in events.json()["data"]["items"]}
    assert "batch.jobs_retried" in event_types
    assert "batch.jobs_skipped" in event_types


@pytest.mark.asyncio
async def test_agent_review_timeline_approval_and_jianying_delivery_flow(
    agent_api,
    monkeypatch,
) -> None:
    story_step = AgentStep(
        id=uuid4(),
        production_id=agent_api.production.id,
        stage="story_bible",
        scope_type="production",
        scope_id=agent_api.production.id,
        status="completed",
        input_version=1,
        output_version=1,
        progress_current=1,
        progress_total=1,
        attempt_count=1,
        extra={},
    )
    core_step = AgentStep(
        id=uuid4(),
        production_id=agent_api.production.id,
        stage="core_assets",
        scope_type="production",
        scope_id=agent_api.production.id,
        status="completed",
        input_version=1,
        output_version=1,
        progress_current=0,
        progress_total=0,
        attempt_count=1,
        extra={},
    )
    bible = SeriesBibleVersion(
        id=uuid4(),
        project_id=agent_api.project.id,
        production_id=agent_api.production.id,
        step_id=story_step.id,
        version=1,
        status="confirmed",
        content={},
        created_by=agent_api.owner.id,
        confirmed_by=agent_api.owner.id,
    )
    core_lock = AgentCoreAssetLock(
        id=uuid4(),
        project_id=agent_api.project.id,
        production_id=agent_api.production.id,
        bible_version_id=bible.id,
        step_id=core_step.id,
        version=1,
        status="active",
        assets=[],
        impact={},
        idempotency_key="review-core-lock-v1",
        created_by=agent_api.owner.id,
    )
    storyboard = ProjectStoryboard(
        id=uuid4(),
        project_id=agent_api.project.id,
        chapter_id=agent_api.chapter.id,
        user_id=agent_api.owner.id,
        ai_model_id=agent_api.text_model.id,
        shot_number=1,
        title="雨夜入宅",
        source_content="沈砚进入旧宅。",
        action="推门进入",
        characters=["沈砚"],
        props=[],
        image_prompt="雨夜旧宅",
        video_prompt="沈砚推门进入旧宅",
        extra={
            "agent_asset_ids": {"character": [], "scene": [], "prop": []},
            "image_generation_status": "success",
            "image_generation_result": "https://example.com/shot-1.png",
            "video_generation_status": "not_started",
            "estimated_duration_seconds": 7,
        },
        is_enabled=True,
    )
    first_history = ProjectGeneratedAsset(
        id=uuid4(),
        project_id=agent_api.project.id,
        chapter_id=agent_api.chapter.id,
        user_id=agent_api.owner.id,
        ai_model_id=agent_api.video_model.id,
        target_type="storyboard",
        target_id=storyboard.id,
        media_type="video",
        result_url="https://example.com/shot-1-v1.mp4",
        result_urls=["https://example.com/shot-1-v1.mp4"],
        status="success",
        is_selected=True,
        extra={},
        is_enabled=True,
    )
    second_history = ProjectGeneratedAsset(
        id=uuid4(),
        project_id=agent_api.project.id,
        chapter_id=agent_api.chapter.id,
        user_id=agent_api.owner.id,
        ai_model_id=agent_api.video_model.id,
        target_type="storyboard",
        target_id=storyboard.id,
        media_type="video",
        result_url="https://example.com/shot-1-v2.mp4",
        result_urls=["https://example.com/shot-1-v2.mp4"],
        status="success",
        is_selected=False,
        extra={},
        is_enabled=True,
    )
    agent_api.chapter.extra = {
        **(agent_api.chapter.extra or {}),
        "storyboard_analysis_status": "success",
    }
    agent_api.production.current_stage = "episode_videos"
    pending_chapter = ProjectChapter(
        id=uuid4(),
        project_id=agent_api.project.id,
        user_id=agent_api.owner.id,
        title="第二集",
        content="沈砚继续调查。",
        processed_content="沈砚继续调查。",
        process_status="completed",
        sort_order=2,
        extra={
            "agent_production_id": str(agent_api.production.id),
            "episode_number": 2,
            "storyboard_analysis_status": "running",
        },
        is_enabled=True,
    )
    agent_api.session.add_all([story_step, core_step])
    await agent_api.session.flush()
    agent_api.session.add(bible)
    await agent_api.session.flush()
    agent_api.session.add_all([core_lock, storyboard, pending_chapter])
    await agent_api.session.commit()
    prefix = f"/api/v1/agent-productions/{agent_api.production.id}"

    workflow_without_video = await agent_api.client.get(f"{prefix}/workflow")
    assert workflow_without_video.status_code == 200
    workflow_data = workflow_without_video.json()["data"]
    assert workflow_data["current_step"] == 3
    assert [item["status"] for item in workflow_data["steps"]] == [
        "completed",
        "completed",
        "processing",
        "waiting_review",
    ]
    assert workflow_data["steps"][3]["can_view"] is True
    timeline_without_video = await agent_api.client.get(
        f"{prefix}/episodes/{agent_api.chapter.id}/video-timeline"
    )
    assert timeline_without_video.status_code == 200
    assert timeline_without_video.json()["data"]["ready_to_approve"] is False
    assert timeline_without_video.json()["data"]["items"][0]["video_status"] == "not_started"
    assert timeline_without_video.json()["data"]["items"][0]["video_url"] is None
    pending_timeline = await agent_api.client.get(
        f"{prefix}/episodes/{pending_chapter.id}/video-timeline"
    )
    assert pending_timeline.status_code == 409
    assert pending_timeline.json()["code"] == 40968

    pending_chapter.is_enabled = False
    await agent_api.session.commit()

    storyboard.extra = {
        **storyboard.extra,
        "video_generation_status": "success",
        "video_generation_result": "https://example.com/shot-1-v1.mp4",
        "video_generation_history_id": str(first_history.id),
    }
    agent_api.session.add_all([first_history, second_history])
    await agent_api.session.commit()

    initial = await agent_api.client.get(f"{prefix}/review")
    assert initial.status_code == 200
    initial_data = initial.json()["data"]
    assert initial_data["approved_episode_count"] == 0
    assert initial_data["episodes"][0]["can_review"] is True
    assert initial_data["episodes"][0]["storyboards"][0]["video_version_count"] == 2

    timeline = await agent_api.client.get(
        f"{prefix}/episodes/{agent_api.chapter.id}/video-timeline"
    )
    assert timeline.status_code == 200
    assert timeline.json()["data"]["ready_to_approve"] is True
    assert timeline.json()["data"]["estimated_duration_seconds"] == 7
    assert timeline.json()["data"]["items"][0]["video_history_id"] == str(first_history.id)

    created_issue = await agent_api.client.post(
        f"{prefix}/review-issues",
        json={
            "chapter_id": str(agent_api.chapter.id),
            "storyboard_id": str(storyboard.id),
            "media_type": "video",
            "category": "motion",
            "severity": "blocking",
            "description": "人物手部动作异常",
        },
    )
    assert created_issue.status_code == 200
    issue_data = created_issue.json()["data"]

    blocked_approval = await agent_api.client.post(
        f"{prefix}/episodes/{agent_api.chapter.id}/approve",
        json={"expected_lock_version": 0, "idempotency_key": "approve-episode-v1"},
    )
    assert blocked_approval.status_code == 409
    assert blocked_approval.json()["code"] == 40973

    resolved = await agent_api.client.patch(
        f"{prefix}/review-issues/{issue_data['id']}",
        json={
            "expected_lock_version": 0,
            "status": "resolved",
            "resolution_note": "已重新检查并接受当前动作",
        },
    )
    assert resolved.status_code == 200
    assert resolved.json()["data"]["lock_version"] == 1

    approved = await agent_api.client.post(
        f"{prefix}/episodes/{agent_api.chapter.id}/approve",
        json={"expected_lock_version": 0, "idempotency_key": "approve-episode-v1"},
    )
    approved_again = await agent_api.client.post(
        f"{prefix}/episodes/{agent_api.chapter.id}/approve",
        json={"expected_lock_version": 0, "idempotency_key": "approve-episode-v1"},
    )
    assert approved.status_code == 200
    assert approved_again.status_code == 200
    assert approved.json()["data"]["lock_version"] == 1

    readiness = await agent_api.client.get(f"{prefix}/delivery-readiness")
    assert readiness.status_code == 200
    assert readiness.json()["data"]["ready"] is True

    delivery_payload = {
        "delivery_type": "manifest",
        "idempotency_key": "delivery-manifest-v1",
    }
    delivery = await agent_api.client.post(f"{prefix}/deliveries", json=delivery_payload)
    delivery_again = await agent_api.client.post(f"{prefix}/deliveries", json=delivery_payload)
    assert delivery.status_code == 200
    assert delivery.json()["data"]["status"] == "completed"
    assert delivery_again.json()["data"]["id"] == delivery.json()["data"]["id"]
    assert delivery.json()["data"]["manifest"]["episodes"][0]["shots"][0][
        "video_url"
    ] == "https://example.com/shot-1-v1.mp4"

    monkeypatch.setattr(
        "app.tasks.agent_delivery.build_agent_delivery.apply_async",
        lambda **_kwargs: None,
    )
    windows_export = await agent_api.client.post(
        f"{prefix}/jianying-exports",
        json={
            "platform": "windows",
            "draft_name": "旧宅谜案",
            "idempotency_key": "jianying-windows-v1",
        },
    )
    macos_export = await agent_api.client.post(
        f"{prefix}/jianying-exports",
        json={
            "platform": "macos",
            "draft_name": "旧宅谜案",
            "idempotency_key": "jianying-macos-v1",
        },
    )
    assert windows_export.status_code == 200
    assert windows_export.json()["data"]["platform"] == "windows"
    assert windows_export.json()["data"]["jianying_version"] == "10.8"
    assert windows_export.json()["data"]["video_count"] == 1
    assert macos_export.status_code == 200
    assert macos_export.json()["data"]["platform"] == "macos"

    export_id = UUID(windows_export.json()["data"]["id"])
    lease_token, retry_after = await claim_agent_delivery(agent_api.session, export_id)
    assert lease_token is not None
    assert retry_after == 0

    async def fake_build_jianying(delivery):
        assert delivery.id == export_id
        return "https://example.com/old-house-windows.zip", {
            "video_count": 1,
            "sha256": "a" * 64,
        }

    monkeypatch.setattr(
        "app.services.jianying_drafts.build_and_upload_jianying_draft",
        fake_build_jianying,
    )
    await run_agent_delivery(agent_api.session, export_id, lease_token)

    completed_export = await agent_api.client.get(f"{prefix}/jianying-exports/{export_id}")
    assert completed_export.status_code == 200
    assert completed_export.json()["data"]["status"] == "completed"
    assert completed_export.json()["data"]["output_url"].endswith("windows.zip")
    exports = await agent_api.client.get(f"{prefix}/jianying-exports")
    assert exports.status_code == 200
    assert {item["platform"] for item in exports.json()["data"]} == {"windows", "macos"}

    await agent_api.session.refresh(agent_api.production)
    assert agent_api.production.status == "completed"
    assert agent_api.production.current_stage == "completed"
