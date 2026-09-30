from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.exceptions import AppException
from app.integrations import comfly, volcengine_ark
from app.integrations.apimart_video_specs import build_video_payload
from app.schemas.project_storyboard import ProjectStoryboardVideoGenerateRequest
from app.services.agent import storyboard_video_inputs as agent_storyboard_video_inputs
from app.services.projects import storyboard_videos as project_storyboard_videos
from app.services.conversation import video_inputs


IMAGE = "https://cdn.example/character.png"
VIDEO = "https://cdn.example/scene.mp4"
AUDIO = "https://cdn.example/voice.mp3"
ARK_MODEL = SimpleNamespace(
    vendor="volcengine_ark", model_id="doubao-seedance-2-0-260128", capabilities={}
)


@pytest.mark.parametrize("url", ["asset://approved-image", "data:image/png;base64,aGVsbG8="])
def test_ark_request_validation_preserves_provider_media_formats(url):
    payload = volcengine_ark.build_video_generation_payload(
        ARK_MODEL.model_id, "人物转身", {"video_mode": "image_to_video", "images": [url]}
    )
    assert payload["content"][1]["image_url"]["url"] == url


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["uploads", "fileList", "attachmentUrls"])
async def test_conversation_unwraps_and_deduplicates_uploaded_references(key):
    extra = await video_inputs.build_video_message_extra(
        {
            "generation_mode": "reference",
            "referenceImageUrl": IMAGE,
            key: [
                {"data": {"response": {"url": IMAGE, "file_type": "image"}}},
                {"file": {"video_url": {"url": VIDEO}, "mime_type": "video/mp4"}},
                {"upload": {"oss_url": AUDIO}},
            ],
        }
    )
    assert extra["images"] == [IMAGE]
    assert extra["videos"] == [VIDEO]
    assert extra["audios"] == [AUDIO]
    assert extra["video_mode"] == "video_to_video"
    assert key not in extra
    assert "referenceImageUrl" not in extra


@pytest.mark.asyncio
@pytest.mark.parametrize("first,last", [
    ("firstFrameUrl", "lastFrameUrl"),
    ("reference_start_frame_url", "reference_end_frame_url"),
])
async def test_frame_aliases_keep_first_then_last_order(first, last):
    extra = await video_inputs.build_video_message_extra(
        {"generation_mode": "first_last_frame", first: IMAGE, last: "https://cdn.example/end.png"}
    )
    payload = build_video_payload("seedance-2.0", "转身", extra)
    assert payload["image_with_roles"] == [
        {"url": IMAGE, "role": "first_frame"},
        {"url": "https://cdn.example/end.png", "role": "last_frame"},
    ]
    assert first not in extra
    assert last not in extra


@pytest.mark.asyncio
async def test_frame_roles_and_reference_roles_stay_separate():
    extra = await video_inputs.build_video_message_extra({
        "generation_mode": "reference",
        "media_items": [
            {"image_url": {"url": IMAGE}, "role": "refImage"},
            {"image_url": {"url": "https://cdn.example/start.png"}, "role": "firstFrame"},
            {"type": "audio_url", "audio_url": AUDIO, "role": "refAudio"},
        ],
    })
    assert extra["images"] == [IMAGE]
    assert extra["audios"] == [AUDIO]
    assert extra["video_mode"] == "audio_video"
    assert "media_items" not in extra


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["conversation", "project"])
async def test_shared_reference_uploads_reach_ark_with_same_media_and_dimensions(monkeypatch, entry):
    extra = {
        "generation_mode": "reference", "resolution": "720p", "duration": 8,
        "ratio": "9:16", "aspect_ratio": "9:16",
        "uploads": [
            {"data": {"url": IMAGE, "type": "image"}},
            {"response": {"url": VIDEO, "type": "video", "media_info": {"duration": 4}}},
            {"file": {"url": AUDIO, "type": "audio"}},
        ],
    }
    if entry == "conversation":
        extra = await video_inputs.build_video_message_extra(extra, ARK_MODEL)
    else:
        extra = project_storyboard_videos._build_storyboard_video_extra(
            SimpleNamespace(generation_ratio="9:16"), ARK_MODEL,
            ProjectStoryboardVideoGenerateRequest(
                ai_model_id=uuid4(), generation_mode="reference", extra=extra,
            ),
            SimpleNamespace(duration_suggestion="5秒"), [], "720p",
        )
    captured = {}

    def create(**payload):
        captured.update(payload)
        return {"id": "provider-task"}

    async def init_client():
        return SimpleNamespace(content_generation=SimpleNamespace(tasks=SimpleNamespace(create=create)))

    monkeypatch.setattr(volcengine_ark, "init_volcengine_ark_client", init_client)
    await volcengine_ark.create_video_generation(ARK_MODEL.model_id, "角色转身", extra)
    assert captured["content"] == [
        {"type": "text", "text": "角色转身"},
        {"type": "image_url", "image_url": {"url": IMAGE}, "role": "reference_image"},
        {"type": "video_url", "video_url": {"url": VIDEO}, "role": "reference_video"},
        {"type": "audio_url", "audio_url": {"url": AUDIO}, "role": "reference_audio"},
    ]
    assert captured["duration"] == 8
    assert captured["ratio"] == "9:16"
    assert captured["resolution"] == "720p"
    assert "uploads" not in captured


@pytest.mark.asyncio
async def test_comfly_preserves_flat_media_url_precedence_and_data_urls(monkeypatch):
    captured = {}

    async def post_json(_path, payload, **_kwargs):
        captured.update(payload)
        return {"id": "provider-task"}

    monkeypatch.setattr(comfly, "_post_json", post_json)
    await comfly.create_video_generation("sora-2", "角色转身", {
        "images": [{"url": IMAGE, "image_url": "https://cdn.example/ignored.png"}],
        "duration": 10,
    })
    assert captured["images"] == [IMAGE]
    await comfly.create_video_generation("sora-2", "角色转身", {
        "images": [{"image_url": {"url": "data:image/png;base64,aGVsbG8="}}],
        "duration": 10,
    })
    assert captured["images"] == ["data:image/png;base64,aGVsbG8="]


@pytest.mark.asyncio
@pytest.mark.parametrize("change,message", [
    ({"content_type": "video/webm"}, "仅支持 mp4 或 mov"),
    ({"size": 200 * 1024 * 1024 + 1}, "不能超过 200MB"),
    ({"media_info": {"duration": 1.79}}, "单个时长"),
    ({"media_info": {"duration": 15.201}}, "单个时长"),
    ({"media_info": {"video_codec": "vp9"}}, "H.264/H.265"),
    ({"media_info": {"audio_codec": "opus"}}, "AAC/MP3"),
    ({"media_info": {"fps": 23}}, "24-60 FPS"),
    ({"media_info": {"fps": 61}}, "24-60 FPS"),
    ({"media_info": {"width": 299, "height": 1600}}, "300-6000px"),
    ({"media_info": {"width": 6001, "height": 1600}}, "300-6000px"),
    ({"media_info": {"width": 300, "height": 300}}, "总像素数"),
    ({"media_info": {"width": 3000, "height": 3000}}, "总像素数"),
    ({"media_info": {"width": 600, "height": 1600}}, "宽高比"),
    ({"media_info": {"width": 1600, "height": 600}}, "宽高比"),
])
async def test_ark_reference_video_restrictions_remain_at_conversation_boundary(change, message):
    item = {"url": VIDEO, "content_type": "video/mp4", "media_info": {"duration": 4}, **change}
    with pytest.raises(AppException, match=message) as exc:
        await video_inputs.build_video_message_extra(
            {"generation_mode": "reference", "uploads": [item]}, ARK_MODEL
        )
    assert exc.value.code == 40012
    assert exc.value.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("duration", [1.8, 2, 15, 15.2])
async def test_ark_reference_video_duration_tolerance_and_duplicate_urls(duration):
    item = {
        "url": VIDEO, "content_type": "video/mp4", "size": 200 * 1024 * 1024,
        "media_info": {"duration": duration, "fps": 60, "width": 1600, "height": 640},
    }
    extra = await video_inputs.build_video_message_extra({
        "generation_mode": "reference", "uploads": [item, item], "reference_video_url": VIDEO,
    }, ARK_MODEL)
    assert extra["videos"] == [VIDEO]


@pytest.mark.asyncio
@pytest.mark.parametrize("duration,accepted", [(7.2, True), (7.201, False)])
async def test_ark_total_reference_duration_limit(duration, accepted):
    request = {
        "generation_mode": "reference",
        "uploads": [
            {"url": VIDEO, "media_info": {"duration": 8}},
            {"url": "https://cdn.example/second.mp4", "media_info": {"duration": duration}},
        ],
    }
    if accepted:
        assert len((await video_inputs.build_video_message_extra(request, ARK_MODEL))["videos"]) == 2
    else:
        with pytest.raises(AppException, match="总时长不能超过 15.2 秒"):
            await video_inputs.build_video_message_extra(request, ARK_MODEL)


@pytest.mark.asyncio
async def test_ark_missing_metadata_is_probed_once_per_reference(monkeypatch):
    calls = []

    async def probe(url, media_type):
        calls.append((url, media_type))
        return {"duration": 4}

    monkeypatch.setattr(video_inputs, "probe_media_url", probe)
    await video_inputs.build_video_message_extra({
        "generation_mode": "reference", "videos": [VIDEO, VIDEO],
    }, ARK_MODEL)
    assert calls == [(VIDEO, "video")]


@pytest.mark.asyncio
@pytest.mark.parametrize("shared_image", [True, False])
async def test_agent_reference_limit_counts_resolved_images_and_keeps_binding_tokens(monkeypatch, shared_image):
    character_id, scene_id = uuid4(), uuid4()
    production = SimpleNamespace(id=uuid4(), project_id=uuid4(), user_id=uuid4())
    storyboard = SimpleNamespace(
        video_prompt="", duration_suggestion="8秒",
        extra={
            "agent_storyboard_prompt_template": "{{asset:scene}}中，{{asset:character}}转身。",
            "agent_asset_bindings": [
                {"binding_key": "character", "asset_type": "character", "asset_id": str(character_id)},
                {"binding_key": "scene", "asset_type": "scene", "asset_id": str(scene_id)},
            ],
        },
    )

    async def load_assets(*_args):
        return {
            "character": {character_id: SimpleNamespace(name="人物", reference_image="character-source")},
            "scene": {scene_id: SimpleNamespace(name="旧宅", reference_image="scene-source")},
            "prop": {},
        }

    async def load_variants(*_args):
        return {}

    async def resolve_urls(_db, urls):
        return [IMAGE if shared_image else f"https://cdn.example/{url}.png" for url in urls]

    monkeypatch.setattr(agent_storyboard_video_inputs, "_load_assets", load_assets)
    monkeypatch.setattr(agent_storyboard_video_inputs, "_load_variants", load_variants)
    monkeypatch.setattr(agent_storyboard_video_inputs, "resolve_storyboard_reference_image_urls", resolve_urls)
    if not shared_image:
        with pytest.raises(AppException, match="最多支持 1 张") as exc:
            await agent_storyboard_video_inputs.build_agent_storyboard_video_input(
                None, production, storyboard, max_images=1,
            )
        assert exc.value.data["required_count"] == 2
        return

    result = await agent_storyboard_video_inputs.build_agent_storyboard_video_input(
        None, production, storyboard, max_images=1,
    )
    assert result.reference_images == [IMAGE]
    assert result.reference_manifest[0]["binding_keys"] == ["scene", "character"]
    assert "旧宅（参考@图片1）" in result.prompt
    assert "人物（参考@图片1）" in result.prompt
    model = SimpleNamespace(vendor="apimart", model_id="seedance-2.0", capabilities={})
    extra = project_storyboard_videos._build_storyboard_video_extra(
        SimpleNamespace(generation_ratio="9:16"), model,
        ProjectStoryboardVideoGenerateRequest(ai_model_id=uuid4(), generation_mode="reference"),
        storyboard, result.reference_images, "720p",
    )
    payload = build_video_payload(model.model_id, result.prompt, extra)
    assert payload["image_urls"] == [IMAGE]
    assert payload["duration"] == 8
    assert payload["size"] == "9:16"
    assert payload["resolution"] == "720p"


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["conversation", "project"])
async def test_first_last_frame_requires_first_frame_at_both_entry_points(entry):
    with pytest.raises(AppException, match="需要传入 first_frame_url"):
        if entry == "conversation":
            await video_inputs.build_video_message_extra({
                "generation_mode": "first_last_frame", "last_frame_url": IMAGE,
            })
        else:
            project_storyboard_videos._build_storyboard_video_extra(
                SimpleNamespace(generation_ratio="9:16"), ARK_MODEL,
                ProjectStoryboardVideoGenerateRequest(
                    ai_model_id=uuid4(), generation_mode="first_last_frame", last_frame_url=IMAGE,
                ),
                SimpleNamespace(duration_suggestion=None), [], "720p",
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("key,count", [("images", 10), ("videos", 4), ("audios", 4)])
async def test_ark_media_count_limits_remain_enforced(key, count):
    extension = {"images": "png", "videos": "mp4", "audios": "mp3"}[key]
    extra = {"generation_mode": "reference", "images": [IMAGE]}
    extra[key] = [f"https://cdn.example/{index}.{extension}" for index in range(count)]
    with pytest.raises(AppException, match="最多支持"):
        await video_inputs.build_video_message_extra(extra, ARK_MODEL)


@pytest.mark.asyncio
async def test_conversation_frames_remove_all_reference_aliases_before_provider_call(monkeypatch):
    model = SimpleNamespace(vendor="comfly", model_id="wan-2.1", configuration={})
    extra = await video_inputs.build_video_message_extra({
        "generation_mode": "first_last_frame",
        "first_frame_url": IMAGE,
        "last_frame_url": "https://cdn.example/end.png",
        "imageUrl": "https://cdn.example/ignored.png",
        "reference_video_url": VIDEO,
        "videos": [VIDEO],
        "audio_url": AUDIO,
        "audioUrls": [AUDIO],
        "uploads": [{"url": VIDEO, "type": "video"}],
    }, model)
    for key in ("imageUrl", "reference_video_url", "videos", "audio_url", "audioUrls", "uploads"):
        assert key not in extra
    captured = {}

    async def post_json(_path, payload, **_kwargs):
        captured.update(payload)
        return {"id": "frame-task"}

    monkeypatch.setattr(comfly, "_post_json", post_json)
    await comfly.create_video_generation(model.model_id, "转身", extra)
    assert captured["images"] == [IMAGE, "https://cdn.example/end.png"]
    assert "audio_url" not in captured and "videos" not in captured


@pytest.mark.asyncio
@pytest.mark.parametrize("media_type,url", [("video", VIDEO), ("audio", AUDIO)])
@pytest.mark.parametrize("container", ["media", "media_items", "content"])
async def test_text_to_video_rejects_roleless_video_and_audio(media_type, url, container):
    with pytest.raises(AppException, match="文生视频不能传入"):
        await video_inputs.build_video_message_extra({
            "generation_mode": "text_to_video",
            container: [{"type": f"{media_type}_url", f"{media_type}_url": {"url": url}}],
        })


@pytest.mark.asyncio
async def test_reference_mode_recognizes_roleless_video_and_audio():
    extra = await video_inputs.build_video_message_extra({
        "media_items": [
            {"type": "video_url", "video_url": {"url": VIDEO}},
            {"type": "audio_url", "audio_url": {"url": AUDIO}},
        ],
    })
    assert extra["generation_mode"] == "reference"
    assert extra["videos"] == [VIDEO]
    assert extra["audios"] == [AUDIO]


@pytest.mark.asyncio
@pytest.mark.parametrize("key,url", [("video_urls", VIDEO), ("audio_urls", AUDIO)])
async def test_reference_mode_rejects_media_that_comfly_grok_would_drop(key, url):
    model = SimpleNamespace(vendor="comfly", model_id="grok-video-3", configuration={})
    with pytest.raises(AppException, match="不支持参考"):
        await video_inputs.build_video_message_extra({
            "generation_mode": "reference", "image_urls": [IMAGE], key: [url],
        }, model)


@pytest.mark.asyncio
async def test_reference_mode_honors_explicit_zero_media_limit():
    model = SimpleNamespace(vendor="custom", model_id="custom", configuration={
        "request": {"capabilities": {
            "modes": ["reference"], "request_keys": ["images", "videos"],
            "media_limits": {"images": 1, "videos": 0},
        }},
    })
    with pytest.raises(AppException, match="不支持参考视频"):
        await video_inputs.build_video_message_extra({
            "generation_mode": "reference", "images": [IMAGE], "videos": [VIDEO],
        }, model)
