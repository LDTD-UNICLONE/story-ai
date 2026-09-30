from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.core.exceptions import AppException
from app.models.agent_production import AgentStep
from app.models.agent_story_bible import AgentAssetVariant, SeriesBibleVersion
from app.models.points import UserPointsTransaction
from app.models.project import Project
from app.models.project_generated_asset import ProjectGeneratedAsset
from app.models.seedance_image import SeedanceImage
from app.models.task_record import UserTaskRecord
from app.schemas.project_storyboard import ProjectStoryboardVideoGenerateRequest
from app.services import seedance_images
from app.services.agent import core_assets
from app.services.generation import task_dispatch
from app.services.projects import image_reviews
from app.services.projects import storyboard_videos
from tests import test_agent_production_integration as agent_fixtures
from tests import test_project_image_reviews_integration as project_fixtures
from tests import test_task_lifecycle_integration as lifecycle_fixtures


lifecycle_db = lifecycle_fixtures.lifecycle_db
project_review = project_fixtures.project_review
agent_api = agent_fixtures.agent_api
pytestmark = project_fixtures.pytestmark


async def submit_agent(ctx):
    url = project_fixtures.upload_data()["url"]
    return await storyboard_videos.submit_storyboard_video_generation(
        ctx.db,
        ctx.seed.project_id,
        ctx.seed.chapter_id,
        ctx.seed.storyboard_id,
        ctx.user,
        ProjectStoryboardVideoGenerateRequest(
            ai_model_id=ctx.seed.model_id,
            generation_mode="reference",
            resolution="720p",
        ),
        agent_context={
            "agent_production_id": str(uuid4()),
            "agent_reference_images": [url],
            "agent_compiled_prompt": "@图片1 中的人物走进房间",
            "agent_reference_manifest": [{"index": 1, "url": url}],
        },
    )


@pytest.mark.parametrize("status", ["unregistered", "pending", "processing", "failed", "uncertain"])
async def test_agent_seedance_rejects_unreviewed_images_before_charging(project_review, status):
    ctx = project_review
    project = await ctx.db.get(Project, ctx.seed.project_id)
    project.project_kind = "agent"
    await ctx.db.commit()
    if status != "unregistered":
        await project_fixtures.review_record(ctx, status)
    before = await ctx.db.scalar(select(func.count()).select_from(UserTaskRecord))
    with pytest.raises(AppException) as error:
        await submit_agent(ctx)
    assert error.value.code == 40016
    assert await ctx.db.scalar(select(func.count()).select_from(UserTaskRecord)) == before
    assert await ctx.db.scalar(select(func.count()).select_from(UserPointsTransaction)) == 0


@pytest.mark.parametrize("model_id", ["seedance-2.0", "seedance-2.0-fast", "seedance-2.5"])
async def test_agent_reuses_review_without_changing_prompt_or_preview(project_review, model_id):
    ctx = project_review
    record = await project_fixtures.review_record(ctx)
    ctx.model.model_id = model_id
    await ctx.db.commit()
    task, _ = await submit_agent(ctx)
    assert task.extra["model_extra"]["image_urls"] == [record.asset_url]
    assert task.extra["reference_images"] == [record.upload["url"]]
    assert task.extra["agent_reference_manifest"][0]["url"] == record.upload["url"]
    assert "@图片1" in task.prompt


@pytest.fixture
async def asset_review(agent_api, monkeypatch):
    api = agent_api
    monkeypatch.setattr(settings, "apimart_api_key", "agent-review-test")
    published, downloads = [], []
    monkeypatch.setattr(task_dispatch, "publish_task_message", lambda **kw: published.append(kw))

    async def download(client, url, **kwargs):
        downloads.append(url)
        return httpx.Response(
            200, content=b"\x89PNG\r\n\x1a\nagent-image", request=httpx.Request("GET", url)
        )

    async def unexpected_upload(*args, **kwargs):
        pytest.fail("Stored Agent images must not be uploaded to OSS again")

    monkeypatch.setattr(image_reviews, "open_safe_http_response", download)
    monkeypatch.setattr(seedance_images, "upload_story_file", unexpected_upload)
    step_id = await api.session.scalar(
        select(AgentStep.id).where(
            AgentStep.production_id == api.production.id,
        )
    )
    bible = SeriesBibleVersion(
        id=uuid4(),
        project_id=api.project.id,
        production_id=api.production.id,
        step_id=step_id,
        version=1,
        status="confirmed",
        content={},
        created_by=api.owner.id,
    )
    api.production.current_stage = "core_assets"
    api.session.add(bible)
    await api.session.commit()
    return SimpleNamespace(api=api, bible=bible, downloads=downloads, published=published)


async def make_asset(ctx, asset_type="character", variant=False):
    api = ctx.api
    prefix = f"/api/v1/agent-productions/{api.production.id}/core-assets"
    response = await api.client.post(
        prefix,
        json={
            "asset_type": asset_type,
            "canonical_name": "审核资产",
            "aliases": [],
            "content": {},
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    asset_id = UUID(data["asset_id"])
    asset = await api.session.get(core_assets.ASSET_MODELS[asset_type], asset_id)
    path = f"{prefix}/{asset_type}/{asset_id}"
    target_type = asset_type
    if variant:
        asset = AgentAssetVariant(
            id=uuid4(),
            project_id=api.project.id,
            production_id=api.production.id,
            bible_version_id=ctx.bible.id,
            base_candidate_id=UUID(data["candidate_id"]),
            user_id=api.owner.id,
            asset_type=asset_type,
            variant_key=uuid4().hex,
            canonical_name="审核变体",
            variant_type={"character": "costume", "scene": "weather", "prop": "form"}[asset_type],
            description="变体描述",
            trigger_reason="剧情变化",
            episode_numbers=[1],
            source_evidence=[],
            confidence=1,
            review_status="ready",
            content={},
            extra={},
            lock_version=0,
        )
        api.session.add(asset)
        path += f"/variants/{asset.id}"
        target_type = "asset_variant"
    asset.reference_image = f"https://example.com/{asset.id}.png"
    await api.session.commit()
    return asset, path + "/reference-image/review", target_type


async def add_history(ctx, asset, target_type, **overrides):
    values = dict(
        project_id=ctx.api.project.id,
        user_id=ctx.api.owner.id,
        target_type=target_type,
        target_id=asset.id,
        media_type="image",
        status="success",
        result_url="https://example.com/first-result.png",
        result_urls=[asset.reference_image],
        is_enabled=True,
        is_selected=False,
        extra={},
    )
    values.update(overrides)
    ctx.api.session.add(ProjectGeneratedAsset(**values))
    await ctx.api.session.commit()


@pytest.mark.parametrize("asset_type", ["character", "scene", "prop"])
@pytest.mark.parametrize("variant", [False, True])
async def test_asset_review_registers_owned_results_once(asset_review, asset_type, variant):
    ctx = asset_review
    asset, path, target_type = await make_asset(ctx, asset_type, variant)
    await add_history(ctx, asset, target_type)
    original_url = asset.reference_image
    first = await ctx.api.client.post(path)
    assert first.status_code == 200, first.text
    data = first.json()["data"]
    assert data["review_status"] == "pending"
    assert data["can_reference"] is False
    second = await ctx.api.client.post(path)
    assert second.status_code == 200, second.text
    assert second.json()["data"]["image_id"] == data["image_id"]
    assert second.json()["data"]["reused"] is True
    assert ctx.downloads == [original_url]
    assert len(ctx.published) == 1
    await ctx.api.session.refresh(asset)
    assert asset.reference_image == original_url
    assert await ctx.api.session.scalar(select(func.count()).select_from(SeedanceImage)) == 1
    assert (
        await ctx.api.session.scalar(select(func.count()).select_from(UserPointsTransaction)) == 0
    )


@pytest.mark.parametrize("status", ["ready", "processing", "failed", "uncertain"])
async def test_asset_review_reuses_uploaded_image_status_without_resubmitting(asset_review, status):
    ctx = asset_review
    asset, path, _ = await make_asset(ctx)
    record = SeedanceImage(
        user_id=ctx.api.owner.id,
        provider_scope=seedance_images.provider_scope(),
        sha256=uuid4().hex,
        upload=project_fixtures.upload_data(asset.reference_image),
        status=status,
        asset_url="asset://existing" if status == "ready" else None,
    )
    ctx.api.session.add(record)
    await ctx.api.session.commit()
    response = await ctx.api.client.post(path)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["image_id"] == str(record.id)
    assert response.json()["data"]["review_status"] == status
    assert response.json()["data"]["can_reference"] == (status == "ready")
    assert ctx.downloads == ctx.published == []


@pytest.mark.parametrize(
    "invalid_source", ["no_history", "other_user", "other_asset", "failed", "disabled"]
)
async def test_asset_review_rejects_unverified_urls_without_downloading(
    asset_review, invalid_source
):
    ctx = asset_review
    asset, path, target_type = await make_asset(ctx)
    overrides = {
        "other_user": {"user_id": ctx.api.outsider.id},
        "other_asset": {"target_id": uuid4()},
        "failed": {"status": "failed"},
        "disabled": {"is_enabled": False},
    }
    if invalid_source != "no_history":
        await add_history(ctx, asset, target_type, **overrides[invalid_source])
    response = await ctx.api.client.post(path)
    assert response.status_code == 400, response.text
    assert response.json()["code"] == 40016
    assert ctx.downloads == ctx.published == []


async def test_asset_review_ownership_and_deleted_project(asset_review):
    ctx = asset_review
    asset, path, target_type = await make_asset(ctx)
    await add_history(ctx, asset, target_type)
    ctx.api.identity["user"] = ctx.api.outsider
    response = await ctx.api.client.post(path)
    assert response.status_code == 404
    ctx.api.identity["user"] = ctx.api.owner
    ctx.api.project.is_enabled = False
    await ctx.api.session.commit()
    response = await ctx.api.client.post(path)
    assert response.status_code == 404
    assert ctx.downloads == ctx.published == []


async def test_same_generated_bytes_share_review_across_assets_and_keep_source_aliases(
    asset_review,
):
    ctx = asset_review
    first_asset, first_path, first_type = await make_asset(ctx)
    await add_history(ctx, first_asset, first_type)
    second_asset, second_path, second_type = await make_asset(ctx, "scene", variant=True)
    await add_history(ctx, second_asset, second_type)
    first = await ctx.api.client.post(first_path)
    second = await ctx.api.client.post(second_path)
    assert first.status_code == second.status_code == 200
    assert first.json()["data"]["image_id"] == second.json()["data"]["image_id"]
    assert second.json()["data"]["reused"] is True
    assert len(ctx.published) == 1
    assert len(ctx.downloads) == 2  # Hash each new source once; no second review or OSS copy.
    record = await ctx.api.session.get(SeedanceImage, UUID(first.json()["data"]["image_id"]))
    assert second_asset.reference_image in record.upload["source_urls"]
    repeated = await ctx.api.client.post(second_path)
    assert repeated.status_code == 200
    assert len(ctx.downloads) == 2


@pytest.mark.parametrize("mismatch", ["other_user", "other_provider"])
async def test_cannot_reuse_another_users_or_providers_review(asset_review, mismatch):
    ctx = asset_review
    asset, path, _ = await make_asset(ctx)
    ctx.api.session.add(
        SeedanceImage(
            user_id=ctx.api.outsider.id if mismatch == "other_user" else ctx.api.owner.id,
            provider_scope="old-provider"
            if mismatch == "other_provider"
            else seedance_images.provider_scope(),
            sha256=uuid4().hex,
            upload=project_fixtures.upload_data(asset.reference_image),
            status="ready",
            asset_url="asset://not-ours",
        )
    )
    await ctx.api.session.commit()
    response = await ctx.api.client.post(path)
    assert response.status_code == 400
    assert ctx.downloads == ctx.published == []


async def test_non_seedance_agent_video_does_not_require_avatar_review(project_review, monkeypatch):
    ctx = project_review
    ctx.model.vendor, ctx.model.model_id = "comfly", "test-video"
    await ctx.db.commit()

    async def unexpected_lookup(*args):
        pytest.fail("Non-Seedance generation must not query the avatar service")

    monkeypatch.setattr(seedance_images, "images_by_urls", unexpected_lookup)
    task, _ = await submit_agent(ctx)
    assert task.status == "pending"
    assert task.extra["reference_images"] == [project_fixtures.upload_data()["url"]]
