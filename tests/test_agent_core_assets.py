from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.core.exceptions import AppException
from app.main import app
from app.schemas.agent_core_asset import (
    CoreAssetConfirmRequest,
    CoreAssetCreateRequest,
    CoreAssetImageGenerationRequest,
    CoreAssetLockRequest,
    CoreAssetReferenceImageRequest,
    CoreAssetSelectionRequest,
    CoreAssetUpdateRequest,
    CoreAssetVariantCreateRequest,
)
from app.services import agent_core_assets as core_asset_service
from app.services.project_asset_generation import build_asset_image_prompt


def test_core_asset_routes_are_registered() -> None:
    routes = {
        (path, method.upper())
        for path, methods in app.openapi()["paths"].items()
        for method in methods
    }

    expected = {
        ("/api/v1/agent-productions/{production_id}/core-assets", "GET"),
        ("/api/v1/agent-productions/{production_id}/core-assets", "POST"),
        (
            "/api/v1/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}",
            "GET",
        ),
        (
            "/api/v1/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}",
            "PATCH",
        ),
        (
            "/api/v1/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}",
            "DELETE",
        ),
        (
            "/api/v1/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}/variants",
            "POST",
        ),
        (
            "/api/v1/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}/variants/{variant_id}",
            "DELETE",
        ),
        (
            "/api/v1/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}/variants/{variant_id}/reference-image",
            "PUT",
        ),
        (
            "/api/v1/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}/variants/{variant_id}/image-generation",
            "POST",
        ),
        ("/api/v1/agent-productions/{production_id}/core-assets/readiness", "GET"),
        ("/api/v1/agent-productions/{production_id}/core-assets/image-generations", "POST"),
        (
            "/api/v1/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}/reference-image",
            "PUT",
        ),
        ("/api/v1/agent-productions/{production_id}/core-assets/confirm", "POST"),
        ("/api/v1/agent-productions/{production_id}/core-assets/impact-preview", "POST"),
        ("/api/v1/agent-productions/{production_id}/core-assets/lock", "POST"),
    }
    assert expected <= routes


def test_core_asset_management_payload_validation() -> None:
    created = CoreAssetCreateRequest(
        asset_type="character",
        canonical_name=" 沈砚 ",
        aliases=["阿砚", "阿砚", " "],
        content={"appearance": "黑发灰眼"},
    )
    assert created.canonical_name == "沈砚"
    assert created.aliases == ["阿砚"]

    with pytest.raises(ValidationError):
        CoreAssetUpdateRequest(expected_lock_version=0)

    with pytest.raises(ValidationError):
        CoreAssetVariantCreateRequest(
            canonical_name="雨夜旧宅",
            variant_type="weather",
            description="暴雨中的旧宅",
            trigger_reason="第一集暴雨",
            episode_numbers=[0],
        )


def test_core_asset_selection_rejects_duplicates_and_invalid_fingerprint() -> None:
    asset_id = uuid4()
    with pytest.raises(ValidationError):
        CoreAssetSelectionRequest(
            expected_lock_version=0,
            assets=[
                {"asset_type": "character", "asset_id": asset_id},
                {"asset_type": "character", "asset_id": asset_id},
            ],
        )


def test_core_asset_image_generation_uses_default_model_when_omitted() -> None:
    payload = CoreAssetImageGenerationRequest(
        items=[
            {
                "asset_type": "character",
                "asset_id": uuid4(),
            }
        ]
    )

    assert payload.ai_model_id is None

    uploaded = CoreAssetReferenceImageRequest(
        expected_lock_version=0,
        reference_image=" https://example.com/uploaded.png ",
    )
    assert uploaded.reference_image == "https://example.com/uploaded.png"

    confirmed = CoreAssetConfirmRequest(
        expected_lock_version=0,
        idempotency_key="core-confirm-v1",
    )
    assert confirmed.idempotency_key == "core-confirm-v1"

    with pytest.raises(ValidationError):
        CoreAssetLockRequest(
            expected_lock_version=1,
            assets=[
                {"asset_type": "character", "asset_id": uuid4()},
                {"asset_type": "scene", "asset_id": uuid4()},
            ],
            idempotency_key="core-lock-v2",
            impact_fingerprint="not-a-sha256",
        )


def test_core_asset_confirmation_does_not_require_reference_images() -> None:
    character_id = uuid4()
    scene_id = uuid4()
    production = SimpleNamespace(id=uuid4())
    context = core_asset_service.CoreAssetContext(
        production=production,
        bible=SimpleNamespace(),
        entries={
            ("character", character_id): core_asset_service.CoreAssetEntry(
                candidate=SimpleNamespace(id=uuid4()),
                asset=SimpleNamespace(name="沈砚", reference_image=None),
            ),
            ("scene", scene_id): core_asset_service.CoreAssetEntry(
                candidate=SimpleNamespace(id=uuid4()),
                asset=SimpleNamespace(name="旧宅", reference_image=None),
            ),
        },
        active_lock=None,
    )
    payload = CoreAssetSelectionRequest(
        expected_lock_version=0,
        assets=[
            {"asset_type": "character", "asset_id": character_id},
            {"asset_type": "scene", "asset_id": scene_id},
        ],
    )

    snapshot = core_asset_service._selection_snapshot(context, payload.assets)

    assert [item["reference_image"] for item in snapshot] == [None, None]


def test_confirm_all_core_assets_still_requires_character_and_scene() -> None:
    character_id = uuid4()
    context = core_asset_service.CoreAssetContext(
        production=SimpleNamespace(id=uuid4()),
        bible=SimpleNamespace(),
        entries={
            ("character", character_id): core_asset_service.CoreAssetEntry(
                candidate=SimpleNamespace(id=uuid4()),
                asset=SimpleNamespace(name="沈砚", reference_image=None),
            )
        },
        active_lock=None,
    )

    with pytest.raises(AppException, match="至少需要选择一个角色和一个场景"):
        core_asset_service._all_asset_snapshot(context)


def test_confirmed_core_assets_still_allow_image_changes_only() -> None:
    context = SimpleNamespace(
        production=SimpleNamespace(
            current_stage="batch_production",
            status="running",
        ),
        active_lock=SimpleNamespace(version=1),
    )

    core_asset_service._require_core_asset_image_editable(context)
    with pytest.raises(AppException):
        core_asset_service._require_core_asset_editable(context)

    context.production.status = "completed"
    core_asset_service._require_core_asset_image_editable(context)


@pytest.mark.asyncio
async def test_core_asset_confirmation_starts_storyboards_for_new_workflow(
    monkeypatch,
) -> None:
    production_id = uuid4()
    user = SimpleNamespace(id=uuid4())
    production = SimpleNamespace(
        id=production_id,
        current_stage="core_assets",
        production_spec={"workflow_version": 2},
        lock_version=4,
        extra={"pending_incremental_storyboard_chapter_ids": [str(uuid4())]},
    )
    supplement_chapter_id = uuid4()
    lock_id = uuid4()
    lock_result = {
        "lock_id": lock_id,
        "version": 3,
        "status": "active",
        "assets": [],
        "impact": {},
        "already_locked": True,
    }
    generated = []

    class Db:
        async def get(self, model, object_id):
            assert model is core_asset_service.AgentProduction
            assert object_id == production_id
            return production

        async def commit(self):
            return None

    async def lock_assets(*_args, **kwargs):
        assert kwargs["auto_select_all"] is True
        return lock_result

    async def incremental_chapter_ids(_db, actual_production):
        assert actual_production is production
        return [supplement_chapter_id]

    async def generate_storyboards(
        _db,
        actual_production_id,
        actual_user,
        payload,
        *,
        chapter_ids,
    ):
        generated.append((actual_production_id, actual_user, payload, chapter_ids))
        return {}

    monkeypatch.setattr(core_asset_service, "lock_core_assets", lock_assets)
    monkeypatch.setattr(
        core_asset_service,
        "_generate_storyboards_after_core_asset_confirmation",
        generate_storyboards,
        raising=False,
    )
    monkeypatch.setattr(
        core_asset_service,
        "_pending_incremental_storyboard_chapter_ids",
        incremental_chapter_ids,
    )

    result = await core_asset_service.confirm_core_assets(
        Db(),
        production_id,
        user,
        CoreAssetConfirmRequest(
            expected_lock_version=2,
            idempotency_key="core-confirm-v3",
        ),
    )

    assert result == lock_result
    assert production.current_stage == "batch_production"
    assert production.lock_version == 5
    assert len(generated) == 1
    actual_production_id, actual_user, payload, chapter_ids = generated[0]
    assert actual_production_id == production_id
    assert actual_user is user
    assert payload.expected_core_asset_lock_version == 3
    assert payload.idempotency_key == f"core-assets-{lock_id}-storyboards"
    assert chapter_ids == [supplement_chapter_id]
    assert "pending_incremental_storyboard_chapter_ids" not in production.extra


@pytest.mark.asyncio
async def test_incremental_storyboard_scope_recovers_for_existing_confirmed_project() -> None:
    production = SimpleNamespace(
        id=uuid4(),
        project_id=uuid4(),
        user_id=uuid4(),
        extra={},
    )
    source_step = SimpleNamespace(id=uuid4(), extra={"incremental": True})
    original = SimpleNamespace(
        id=uuid4(),
        extra={"agent_step_id": str(uuid4()), "episode_number": 1},
    )
    supplement = SimpleNamespace(
        id=uuid4(),
        extra={"agent_step_id": str(source_step.id), "episode_number": 2},
    )

    class Result:
        def __init__(self, *, scalar=None, scalars=None):
            self.scalar = scalar
            self.values = scalars or []

        def scalar_one_or_none(self):
            return self.scalar

        def scalars(self):
            return self

        def all(self):
            return self.values

    class Db:
        def __init__(self):
            self.results = [
                Result(scalar=source_step),
                Result(scalars=[original, supplement]),
            ]

        async def execute(self, _query):
            return self.results.pop(0)

    chapter_ids = await core_asset_service._pending_incremental_storyboard_chapter_ids(
        Db(),
        production,
    )

    assert chapter_ids == [supplement.id]


def test_asset_image_prompt_includes_batch_item_custom_requirement() -> None:
    project = SimpleNamespace(style=SimpleNamespace(prompt="写实漫剧"))
    asset = SimpleNamespace(prompt="黑发，灰色长风衣", description=None, name="沈砚")

    prompt = build_asset_image_prompt(
        project,
        asset,
        "character",
        "general",
        "保持正面全身构图",
    )

    assert "用户补充要求：保持正面全身构图" in prompt


def test_variant_image_prompt_requires_base_reference_and_visible_delta() -> None:
    project = SimpleNamespace(style=SimpleNamespace(prompt="写实漫剧"))
    asset = SimpleNamespace(
        name="沈砚",
        identity="调查员",
        gender="男",
        age="28岁",
        appearance="黑色短发",
        costume="灰色长风衣",
        personality="沉稳",
        description=None,
        prompt=None,
    )
    variant = SimpleNamespace(
        canonical_name="沈砚受伤造型",
        description="额角伤口与血迹",
        trigger_reason="遭到袭击",
        content={
            "visual_delta": {"add": ["额角伤口"], "replace": [], "remove": []},
            "preserve_anchors": ["黑色短发", "窄长脸"],
        },
    )

    prompt = core_asset_service.build_asset_variant_image_prompt(
        project,
        asset,
        variant,
        "character",
    )

    assert "沈砚受伤造型" in prompt
    assert "额角伤口" in prompt
    assert "黑色短发" in prompt
    assert "必须继承基础资产参考图" in prompt


@pytest.mark.parametrize(
    ("asset_type", "required_phrases"),
    [
        (
            "character",
            ("16:9", "白色背景", "左侧", "人物肖像", "右侧", "正面、侧面、背面"),
        ),
        ("scene", ("16:9", "单幅场景图", "完整空间结构")),
        (
            "prop",
            ("16:9", "白色背景", "左侧", "道具完整图", "右侧", "细节角度"),
        ),
    ],
)
def test_agent_asset_general_prompts_define_fixed_layouts(
    asset_type: str,
    required_phrases: tuple[str, ...],
) -> None:
    project = SimpleNamespace(style=SimpleNamespace(prompt="写实漫剧"))
    asset = SimpleNamespace(prompt="资产视觉描述", description=None, name="资产")

    prompt = build_asset_image_prompt(project, asset, asset_type, "general")

    assert all(phrase in prompt for phrase in required_phrases)


@pytest.mark.parametrize(
    ("asset_type", "asset", "required_values"),
    [
        (
            "character",
            SimpleNamespace(
                name="沈砚",
                prompt=None,
                description="克制冷静",
                identity="调查员",
                gender="男",
                age="28岁",
                appearance="黑发灰眼",
                costume="灰色长风衣",
                personality="沉稳",
            ),
            ("沈砚", "调查员", "黑发灰眼", "灰色长风衣", "沉稳"),
        ),
        (
            "scene",
            SimpleNamespace(
                name="旧宅",
                prompt=None,
                description="废弃宅院",
                location="城郊",
                time_of_day="夜晚",
                environment="木质回廊",
                atmosphere="阴冷",
            ),
            ("旧宅", "城郊", "夜晚", "木质回廊", "阴冷"),
        ),
        (
            "prop",
            SimpleNamespace(
                name="玉佩",
                prompt=None,
                description="家族信物",
                category="饰品",
                appearance="青白玉，边缘有裂纹",
                function="开启暗门",
            ),
            ("玉佩", "饰品", "青白玉，边缘有裂纹", "开启暗门"),
        ),
    ],
)
def test_asset_image_prompt_uses_all_analyzed_asset_details(
    asset_type: str,
    asset: SimpleNamespace,
    required_values: tuple[str, ...],
) -> None:
    project = SimpleNamespace(style=SimpleNamespace(prompt="写实漫剧"))

    prompt = build_asset_image_prompt(project, asset, asset_type, "general")

    assert all(value in prompt for value in required_values)


@pytest.mark.asyncio
async def test_agent_core_asset_generation_forces_16_9(monkeypatch) -> None:
    production_id = uuid4()
    project_id = uuid4()
    user = SimpleNamespace(id=uuid4())
    asset_id = uuid4()
    asset = SimpleNamespace(id=asset_id, name="沈砚", extra={})
    context = SimpleNamespace(
        production=SimpleNamespace(
            id=production_id,
            project_id=project_id,
            status="planning",
            max_points=None,
            consumed_points=0,
            production_spec={"workflow_version": 2},
        ),
        entries={
            ("character", asset_id): SimpleNamespace(
                candidate=SimpleNamespace(id=uuid4()),
                asset=asset,
            )
        },
    )
    image_model = SimpleNamespace(id=uuid4())
    task_record = SimpleNamespace(
        id=uuid4(),
        status="pending",
        generation_type="asset_image_generate",
        extra={},
    )
    captured = {}

    class FakeDB:
        async def execute(self, _statement):
            return None

        async def commit(self):
            return None

    async def fake_context(*_args, **_kwargs):
        return context

    async def fake_models(*_args, **_kwargs):
        return {"image": image_model}

    async def fake_points(*_args, **_kwargs):
        return None

    async def fake_submit(*_args, **kwargs):
        captured.update(kwargs)
        return asset, task_record, 0

    monkeypatch.setattr(core_asset_service, "_get_context", fake_context)
    monkeypatch.setattr(core_asset_service, "resolve_fixed_agent_models", fake_models)
    monkeypatch.setattr(core_asset_service, "ensure_user_points_enough", fake_points)
    monkeypatch.setattr(core_asset_service, "calculate_submission_points_cost", lambda *_: 0)
    monkeypatch.setattr(core_asset_service, "submit_asset_image_generation", fake_submit)
    monkeypatch.setattr(core_asset_service, "provider_next_poll_seconds", lambda *_: 10)

    result = await core_asset_service.submit_core_asset_image_generations(
        FakeDB(),
        production_id,
        user,
        CoreAssetImageGenerationRequest(
            items=[{"asset_type": "character", "asset_id": asset_id}]
        ),
    )

    assert result["submitted_count"] == 1
    assert captured["aspect_ratio"] == "16:9"
