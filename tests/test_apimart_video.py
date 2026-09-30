from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from openai import BadRequestError

from app.core.exceptions import AppException
from app.api.v1.endpoints.admin.models import normalize_provider_model
from app.integrations import apimart
from app.integrations.apimart_video_specs import (
    build_video_payload,
    video_model_capabilities,
)
from app.services.models.catalog import resolve_ai_model_capabilities
from app.services.conversation.video_inputs import build_video_message_extra
from app.services.generation.runner import run_model
from app.services.billing.model_points import calculate_video_model_points_cost
from app.services.projects.storyboard_videos import (
    _build_storyboard_video_extra,
    _normalize_storyboard_video_resolution,
    storyboard_video_image_limit,
)
from app.schemas.project_storyboard import ProjectStoryboardVideoGenerateRequest


class _RawResponse:
    def __init__(self, payload):
        self._payload = payload
        self.headers = {"content-type": "application/json"}

    async def aread(self):
        return b""

    def json(self):
        return self._payload

    async def aclose(self):
        return None


class _Client:
    def __init__(self, payload):
        self.payload = payload
        self.captured = {}

    async def post(self, path, **kwargs):
        self.captured = {"path": path, **kwargs}
        return _RawResponse(self.payload)


class _FailingClient:
    def __init__(self, error):
        self.error = error

    async def post(self, path, **kwargs):
        raise self.error


def test_seedance_25_builds_multimodal_video_payload() -> None:
    payload = build_video_payload(
        "seedance-2.5",
        "@图片1 走入 @视频1 的环境",
        {
            "image_urls": ["asset://avatar-1"],
            "video_urls": ["https://cdn.example/reference.mp4"],
            "audio_urls": ["https://cdn.example/voice.mp3"],
            "duration": 12,
            "size": "9:16",
            "resolution": "1080P",
            "generate_audio": True,
            "watermark": True,
            "output_format": "mov",
        },
    )

    assert payload == {
        "model": "seedance-2.5",
        "prompt": "@图片1 走入 @视频1 的环境",
        "duration": 12,
        "size": "9:16",
        "resolution": "1080p",
        "image_urls": ["asset://avatar-1"],
        "video_urls": ["https://cdn.example/reference.mp4"],
        "audio_urls": ["https://cdn.example/voice.mp3"],
        "generate_audio": True,
        "nsfw_check": True,
        "watermark": False,
        "output_format": "mov",
    }


def test_seedance_20_rejects_frames_mixed_with_video() -> None:
    with pytest.raises(AppException, match="首尾帧不能与参考视频或音频同时使用"):
        build_video_payload(
            "seedance-2.0",
            "角色转身",
            {
                "first_frame_url": "https://cdn.example/first.png",
                "video_urls": ["https://cdn.example/reference.mp4"],
            },
        )


def test_seedance_allows_explicit_nsfw_check_disable() -> None:
    payload = build_video_payload(
        "seedance-2.0",
        "角色转身",
        {"nsfw_check": False},
    )

    assert payload["nsfw_check"] is False


def test_minimax_h3_normalizes_documented_resolution() -> None:
    payload = build_video_payload(
        "MiniMax-H3",
        "电影感追逐镜头",
        {"resolution": "768p", "aspect_ratio": "21:9", "duration": 15},
    )

    assert payload["resolution"] == "768P"
    assert payload["aspect_ratio"] == "21:9"
    assert payload["duration"] == 15


def test_pixverse_first_last_frame_requires_supported_duration() -> None:
    with pytest.raises(AppException, match="duration 仅支持 5 或 8"):
        build_video_payload(
            "pixverse-v6",
            "镜头从白天过渡到夜晚",
            {
                "first_frame_url": "https://cdn.example/first.png",
                "last_frame_url": "https://cdn.example/last.png",
                "duration": 7,
            },
        )


def test_gemini_omni_rejects_uncontrollable_duration() -> None:
    with pytest.raises(AppException, match="不支持 duration 参数"):
        build_video_payload(
            "gemini-omni-flash-preview",
            "让图片产生自然运动",
            {
                "image_urls": ["https://cdn.example/frame.png"],
                "aspect_ratio": "16:9",
                "resolution": "720p",
                "duration": 8,
            },
        )


def test_gemini_omni_rejects_first_last_frame_mode() -> None:
    with pytest.raises(AppException, match="不支持首尾帧模式"):
        build_video_payload(
            "gemini-omni-flash-preview",
            "镜头平滑过渡",
            {"first_frame_url": "https://cdn.example/first.png"},
        )


def test_seedance_25_requires_prompt_for_reference_generation() -> None:
    with pytest.raises(AppException, match="prompt 不能为空"):
        build_video_payload(
            "seedance-2.5",
            "",
            {"image_urls": ["https://cdn.example/reference.png"]},
        )


@pytest.mark.asyncio
async def test_create_apimart_video_generation_uses_unified_endpoint(monkeypatch) -> None:
    client = _Client({"code": 200, "data": [{"status": "submitted", "task_id": "video-task-1"}]})

    async def get_client():
        return client

    monkeypatch.setattr(apimart, "_get_client", get_client)

    result = await apimart.create_video_generation(
        "seedance-2.0",
        "角色奔跑",
        {"duration": 5, "resolution": "720p"},
        idempotency_key="record-video-1",
    )

    assert result["data"][0]["task_id"] == "video-task-1"
    assert client.captured["path"] == "/videos/generations"
    assert client.captured["cast_to"] is httpx.Response
    assert client.captured["options"] == {"idempotency_key": "record-video-1"}


@pytest.mark.asyncio
async def test_seedance_nsfw_rejection_returns_actionable_error(monkeypatch) -> None:
    request = httpx.Request("POST", "https://api.apib.ai/v1/videos/generations")
    response = httpx.Response(400, request=request)
    error = BadRequestError(
        "rejected",
        response=response,
        body={
            "error": {
                "code": "nsfw_content_detected",
                "type": "nsfw_content_detected",
            }
        },
    )

    async def get_client():
        return _FailingClient(error)

    monkeypatch.setattr(apimart, "_get_client", get_client)

    with pytest.raises(AppException, match="未通过内容审核") as exc_info:
        await apimart.create_video_generation(
            "seedance-2.0",
            "角色行走",
            {},
        )

    assert exc_info.value.code == 40019


@pytest.mark.asyncio
async def test_model_runner_routes_apimart_video_generation(monkeypatch) -> None:
    captured = {}

    async def create_video_generation(model, prompt, extra, *, idempotency_key=None):
        captured.update(
            model=model,
            prompt=prompt,
            extra=extra,
            idempotency_key=idempotency_key,
        )
        return {
            "code": 200,
            "data": [{"status": "submitted", "task_id": "video-task-2"}],
        }

    monkeypatch.setattr(apimart, "create_video_generation", create_video_generation)
    model = SimpleNamespace(
        model_id="seedance-2.5",
        vendor="apimart",
        capabilities={},
    )

    result = await run_model(
        model,
        "video",
        "电影感镜头",
        {"duration": 10},
        idempotency_key="record-video-2",
    )

    assert result.content == "视频生成任务已提交：video-task-2"
    assert result.extra["task_id"] == "video-task-2"
    assert result.extra["task_status"] == "submitted"
    assert captured["extra"]["_model_capabilities"]["model_family"] == "seedance_2_5"


@pytest.mark.asyncio
async def test_conversation_allows_seedance_25_audio_only_reference() -> None:
    model = SimpleNamespace(
        model_id="seedance-2.5",
        vendor="apimart",
        capabilities={},
    )

    extra = await build_video_message_extra(
        {
            "audio_urls": ["https://cdn.example/dialogue.mp3"],
            "generation_mode": "reference",
        },
        model,
    )

    assert extra["video_mode"] == "audio_video"
    assert extra["resolution"] == "720p"
    assert extra["audio_urls"] == ["https://cdn.example/dialogue.mp3"]


def test_apimart_video_capabilities_are_resolved_from_model_contract() -> None:
    model = SimpleNamespace(
        model_id="pixverse-v6",
        vendor="apimart",
        model_type="video",
        configuration={
            "request": {
                "capabilities": {
                    "display_group": "APIMart 视频",
                    "resolutions": ["invalid"],
                }
            }
        },
    )

    capabilities = resolve_ai_model_capabilities(model)

    assert capabilities["display_group"] == "APIMart 视频"
    assert capabilities["resolutions"] == ["1080p", "360p", "540p", "720p"]
    assert capabilities["media_limits"] == {"images": 7, "videos": 0, "audios": 0}
    assert capabilities == video_model_capabilities("pixverse-v6") | {
        "display_group": "APIMart 视频"
    }


def test_apimart_video_capabilities_expose_model_duration_contracts() -> None:
    assert video_model_capabilities("seedance-2.0")["duration"] == {
        "min": 4,
        "max": 15,
        "default": 5,
        "controllable": True,
    }
    assert video_model_capabilities("seedance-2.5")["duration"] == {
        "min": 4,
        "max": 30,
        "default": 5,
        "special": [-1],
        "controllable": True,
    }
    assert video_model_capabilities("pixverse-v6")["duration"] == {
        "min": 1,
        "max": 15,
        "default": 5,
        "mode_values": {"first_last_frame": [5, 8]},
        "controllable": True,
    }
    assert video_model_capabilities("gemini-omni-flash-preview")["duration"] == {
        "min": 3,
        "max": 10,
        "controllable": False,
        "provider_managed": True,
    }
    seedance_25_keys = video_model_capabilities("seedance-2.5")["request_keys"]
    assert "watermark" not in seedance_25_keys


def test_apimart_video_billing_uses_model_specific_duration_range() -> None:
    seedance = SimpleNamespace(model_id="seedance-2.5", vendor="apimart")
    pixverse = SimpleNamespace(model_id="pixverse-v6", vendor="apimart")

    assert calculate_video_model_points_cost(seedance, {"duration": 30}) == 300
    assert calculate_video_model_points_cost(seedance, {"duration": -1}) == 300
    assert calculate_video_model_points_cost(pixverse, {"duration": 1}) == 10


def test_project_and_agent_storyboard_video_use_apimart_limits() -> None:
    model = SimpleNamespace(
        model_id="MiniMax-H3",
        vendor="apimart",
        capabilities={},
    )

    assert storyboard_video_image_limit(model) == 9
    assert _normalize_storyboard_video_resolution(model, "768p") == "768P"

    request = ProjectStoryboardVideoGenerateRequest(
        ai_model_id=uuid4(),
        resolution="2K",
    )
    assert request.resolution == "2K"


def test_project_storyboard_video_uses_apimart_model_duration_range() -> None:
    project = SimpleNamespace(generation_ratio="16:9")
    model = SimpleNamespace(
        model_id="seedance-2.5",
        vendor="apimart",
        capabilities={},
    )
    storyboard = SimpleNamespace(duration_suggestion="20秒")
    request = ProjectStoryboardVideoGenerateRequest(
        ai_model_id=uuid4(),
        generation_mode="text_to_video",
        resolution="720p",
    )

    extra = _build_storyboard_video_extra(
        project,
        model,
        request,
        storyboard,
        [],
        "720p",
    )

    assert extra["duration"] == 20
    assert extra["duration_seconds"] == 20


def test_admin_apimart_video_discovery_filters_non_video_models() -> None:
    assert normalize_provider_model({"id": "gpt-5"}, "video", "apimart") is None

    item = normalize_provider_model(
        {"id": "gemini-omni-flash-preview"},
        "video",
        "apimart",
    )

    assert item is not None
    assert item["vendor"] == "apimart"
    assert item["configuration"]["request"]["capabilities"]["resolutions"] == ["720p"]
