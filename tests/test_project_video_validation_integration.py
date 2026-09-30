import os
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select

from app.core.exceptions import AppException
from app.integrations import apimart, comfly, volcengine_ark
from app.models.points import UserPointsTransaction
from app.models.project_storyboard import ProjectStoryboard
from app.models.task_dispatch import TaskDispatchOutbox
from app.models.task_record import UserTaskRecord
from app.schemas.project_storyboard import ProjectStoryboardVideoGenerateRequest
from app.services.generation.runner import run_model
from app.services.generation import task_dispatch
from app.services.models.configuration import build_model_runtime_snapshot
from tests.test_project_image_reviews_integration import project_review, submit  # noqa: F401
from tests.test_task_lifecycle_integration import lifecycle_db  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.skipif(os.getenv("RUN_DB_INTEGRATION_TESTS") != "1", reason="isolated PostgreSQL"),
]

PROVIDERS = [
    ("apimart", "pixverse-v6"),
    ("comfly", "wan-2.1"),
    ("volcengine_ark", "doubao-seedance-2-0-260128"),
]
MODES = ["text_to_video", "reference", "first_last_frame", "storyboard"]
IMAGE = "https://example.com/reference.png"


async def prepare(ctx, vendor, model_id, mode, *, url=IMAGE, extra=None, resolution="720p"):
    ctx.model.vendor, ctx.model.model_id = vendor, model_id
    sb = await ctx.db.get(ProjectStoryboard, ctx.seed.storyboard_id)
    sb.video_prompt = "人物转身"
    sb.extra = {"image_generation_result": url}
    await ctx.db.commit()
    return ProjectStoryboardVideoGenerateRequest(
        ai_model_id=ctx.seed.model_id,
        generation_mode=mode,
        uploaded_images=[url] if mode == "reference" and url else [],
        first_frame_url=url if mode == "first_last_frame" else None,
        last_frame_url="https://example.com/end.png"
        if mode == "first_last_frame" and url
        else None,
        resolution=resolution,
        extra=extra,
    )


async def assert_rejected_without_side_effects(ctx, payload, status=400):
    tables = (UserTaskRecord, UserPointsTransaction, TaskDispatchOutbox)
    before = [await ctx.db.scalar(select(func.count()).select_from(table)) for table in tables]
    balance = ctx.user.points_balance
    sb = await ctx.db.get(ProjectStoryboard, ctx.seed.storyboard_id)
    original_extra = dict(sb.extra)
    with pytest.raises(AppException) as error:
        await submit(ctx, payload)
    assert error.value.status_code == status
    await ctx.db.flush()
    assert [
        await ctx.db.scalar(select(func.count()).select_from(table)) for table in tables
    ] == before
    await ctx.db.refresh(ctx.user)
    await ctx.db.refresh(sb)
    assert ctx.user.points_balance == balance
    assert sb.extra == original_extra


@pytest.mark.parametrize("vendor,model_id", PROVIDERS)
@pytest.mark.parametrize("mode", MODES[1:])
async def test_invalid_image_url_is_rejected_before_charge(project_review, vendor, model_id, mode):  # noqa: F811
    # The storyboard loader accepts only HTTP-prefixed saved URLs; use a malformed
    # HTTP URL here to exercise the provider validator rather than its missing-image check.
    payload = await prepare(project_review, vendor, model_id, mode, url="https://")
    await assert_rejected_without_side_effects(project_review, payload)


@pytest.mark.parametrize("vendor,model_id", PROVIDERS)
@pytest.mark.parametrize("mode", MODES)
async def test_invalid_parameters_are_rejected_before_charge(
    project_review, vendor, model_id, mode  # noqa: F811
):
    payload = await prepare(project_review, vendor, model_id, mode, extra={"duration": True})
    await assert_rejected_without_side_effects(project_review, payload)


@pytest.mark.parametrize("vendor,model_id", PROVIDERS)
@pytest.mark.parametrize("mode", MODES[1:])
async def test_required_media_is_checked_before_charge(project_review, vendor, model_id, mode):  # noqa: F811
    payload = await prepare(project_review, vendor, model_id, mode, url="")
    await assert_rejected_without_side_effects(project_review, payload)


@pytest.mark.parametrize("vendor,model_id", PROVIDERS)
async def test_unsupported_resolution_is_rejected_before_charge(project_review, vendor, model_id):  # noqa: F811
    payload = await prepare(project_review, vendor, model_id, "text_to_video", resolution="2K")
    await assert_rejected_without_side_effects(project_review, payload)


@pytest.mark.parametrize("vendor,model_id", PROVIDERS)
@pytest.mark.parametrize("mode", MODES)
async def test_valid_submission_matches_worker_provider_payload(
    project_review,  # noqa: F811
    monkeypatch,
    vendor,
    model_id,
    mode,
):
    ctx = project_review
    payload = await prepare(ctx, vendor, model_id, mode, extra={"duration": 5})
    provider = {"apimart": apimart, "comfly": comfly, "volcengine_ark": volcengine_ark}[vendor]
    build = provider.build_video_generation_payload
    built = []
    sent = []
    published = []

    def capture_build(*args):
        result = build(*args)
        built.append(deepcopy(result))
        return result

    monkeypatch.setattr(provider, "build_video_generation_payload", capture_build)
    monkeypatch.setattr(
        task_dispatch, "publish_task_message", lambda **kwargs: published.append(kwargs)
    )
    task, _ = await submit(ctx, payload)
    assert len(built) == 1
    assert await ctx.db.scalar(select(func.count()).select_from(UserPointsTransaction)) == 1
    assert len(published) == 1

    async def post_json(path, body, **kwargs):
        sent.append(body)
        return {"id": "provider-task", "status": "pending"}

    async def apimart_post(path, *, body, **kwargs):
        sent.append(body)
        return httpx.Response(
            200, json={"data": [{"task_id": "provider-task", "status": "pending"}]}
        )

    def ark_create(**body):
        sent.append(body)
        return {"id": "provider-task", "status": "pending"}

    async def apimart_client():
        return SimpleNamespace(post=apimart_post)

    async def ark_client():
        return SimpleNamespace(
            content_generation=SimpleNamespace(tasks=SimpleNamespace(create=ark_create))
        )

    monkeypatch.setattr(comfly, "_post_json", post_json)
    monkeypatch.setattr(apimart, "_get_client", apimart_client)
    monkeypatch.setattr(volcengine_ark, "init_volcengine_ark_client", ark_client)
    await run_model(
        build_model_runtime_snapshot(ctx.model), "video", task.prompt, task.extra["model_extra"]
    )
    assert built == [sent[0], sent[0]]


@pytest.mark.parametrize(
    "vendor,model_id,value",
    [
        ("apimart", "pixverse-v6", 16),
        ("comfly", "seedance-1.0", 8),
        ("comfly", "wan-2.1", "five"),
        ("comfly", "wan-2.1", 0),
        ("volcengine_ark", "doubao-seedance-2-0-260128", 16),
        ("volcengine_ark", "doubao-seedance-2-0-260128", 5.5),
    ],
)
async def test_explicit_duration_is_not_clamped_or_ignored(
    project_review,  # noqa: F811
    vendor,
    model_id,
    value,
):
    payload = await prepare(
        project_review, vendor, model_id, "text_to_video", extra={"seconds": value}
    )
    await assert_rejected_without_side_effects(project_review, payload)


@pytest.mark.parametrize(
    "vendor,model_id,value",
    [
        ("comfly", "sora-2-pro", 25),
        ("volcengine_ark", "doubao-seedance-2-0-260128", -1),
    ],
)
async def test_provider_special_duration_remains_available(
    project_review,  # noqa: F811
    vendor,
    model_id,
    value,
):
    payload = await prepare(
        project_review, vendor, model_id, "text_to_video", extra={"duration": value}
    )
    task, _ = await submit(project_review, payload)
    assert task.extra["model_extra"]["duration"] == value


async def test_explicit_duration_takes_precedence_over_storyboard_suggestion(project_review):  # noqa: F811
    ctx = project_review
    payload = await prepare(
        ctx, "apimart", "pixverse-v6", "first_last_frame", extra={"duration": 5}
    )
    sb = await ctx.db.get(ProjectStoryboard, ctx.seed.storyboard_id)
    sb.duration_suggestion = "15秒"
    await ctx.db.commit()
    task, _ = await submit(ctx, payload)
    assert task.extra["model_extra"]["duration"] == 5


@pytest.mark.parametrize("url", ["not-a-url", "https://[invalid", "https://example.com/a b.png"])
@pytest.mark.parametrize("vendor,model_id", PROVIDERS)
async def test_malformed_frame_urls_return_parameter_error(
    project_review,  # noqa: F811
    vendor,
    model_id,
    url,
):
    payload = await prepare(project_review, vendor, model_id, "first_last_frame", url=url)
    await assert_rejected_without_side_effects(project_review, payload)


@pytest.mark.parametrize(
    "vendor,model_id",
    [
        ("apimart", "gemini-omni-flash-preview"),
        ("comfly", "sora-2"),
    ],
)
async def test_unsupported_frame_mode_is_rejected(project_review, vendor, model_id):  # noqa: F811
    payload = await prepare(project_review, vendor, model_id, "first_last_frame")
    await assert_rejected_without_side_effects(project_review, payload)


async def test_unsupported_reference_video_is_not_silently_dropped(project_review):  # noqa: F811
    payload = await prepare(
        project_review,
        "comfly",
        "wan-2.1",
        "reference",
        extra={"videos": ["https://example.com/video.mp4"]},
    )
    await assert_rejected_without_side_effects(project_review, payload)


async def test_top_level_mode_controls_provider_mode(project_review):  # noqa: F811
    payload = await prepare(
        project_review,
        "volcengine_ark",
        "doubao-seedance-2-0-260128",
        "reference",
        extra={"generation_mode": "text_to_video"},
    )
    task, _ = await submit(project_review, payload)
    assert task.extra["model_extra"]["generation_mode"] == "reference"


@pytest.mark.parametrize("case", ["unknown_provider", "maintenance"])
async def test_unavailable_model_is_rejected_before_charge(project_review, case):  # noqa: F811
    ctx = project_review
    payload = await prepare(ctx, "comfly", "wan-2.1", "text_to_video")
    if case == "unknown_provider":
        ctx.model.vendor = "unknown"
    else:
        ctx.model.configuration = {
            **ctx.model.configuration,
            "operations": {"status": "maintenance"},
        }
    await ctx.db.commit()
    await assert_rejected_without_side_effects(
        ctx, payload, status=503 if case == "maintenance" else 400
    )
