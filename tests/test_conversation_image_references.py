import pytest
from pydantic import ValidationError

from app.core.exceptions import AppException
from app.integrations import apimart
from app.schemas.conversation import ConversationSendMessageRequest
from app.services.conversation.image_references import compile_image_references
from app.services.conversation.video_inputs import build_video_message_extra


REFERENCES = [
    {"name": "人物图", "url": "https://cdn.example/character.png"},
    {"name": "场景图", "url": "https://cdn.example/scene.png"},
]


def test_named_images_preserve_user_instructions_and_provider_image_order():
    payload = ConversationSendMessageRequest(
        content="让 @人物图 站在 @场景图 中，保持 @人物图 的服装。",
        image_references=REFERENCES,
    )
    prompt, extra = compile_image_references(
        payload.content, {}, payload.image_references, "image",
    )
    provider = apimart.build_image_generation_payload("gpt-image-2", prompt, extra)
    assert provider["image_urls"] == [item["url"] for item in REFERENCES]
    assert provider["prompt"] == "让 @图片1 站在 @图片2 中，保持 @图片1 的服装。"
    assert "@人物图" not in prompt and "@场景图" not in prompt
    assert payload.content.startswith("让 @人物图")
    assert "image_references" not in extra


def test_braced_mentions_and_repeated_urls_keep_one_image_index():
    refs = [REFERENCES[0], {**REFERENCES[1], "url": REFERENCES[0]["url"]}]
    payload = ConversationSendMessageRequest(
        content="让@{人物图}站在@{场景图}中。", image_references=refs,
    )
    prompt, extra = compile_image_references(payload.content, {}, payload.image_references, "image")
    assert prompt == "让@图片1站在@图片1中。"
    assert extra["image_urls"] == [REFERENCES[0]["url"]]


@pytest.mark.parametrize("reference", [
    {**REFERENCES[0], "role": "unknown"},
    {**REFERENCES[0], "role": "character"},
    {**REFERENCES[0], "role": "scene"},
    {**REFERENCES[0], "url": "not-a-url"},
    {**REFERENCES[0], "name": ""},
    {**REFERENCES[0], "name": "人物 图"},
    {**REFERENCES[0], "role": "character", "unexpected": True},
])
def test_reference_schema_rejects_invalid_fields(reference):
    with pytest.raises(ValidationError):
        ConversationSendMessageRequest(content="@人物图", image_references=[reference])


def test_reference_names_must_be_unique():
    with pytest.raises(ValidationError):
        ConversationSendMessageRequest(content="@人物图", image_references=[REFERENCES[0]] * 2)


@pytest.mark.parametrize("content,extra,message_type", [
    ("@人物图", {}, "text"),
    ("@人物图背面", {}, "image"),
    ("@人物图 和 @未绑定", {}, "image"),
    ("@人物图 和@{未绑定}", {}, "image"),
    ("@人物图", {"uploaded_images": ["https://cdn.example/other.png"]}, "image"),
    ("@人物图", {"media_items": [{"image_url": "https://cdn.example/other.png"}]}, "video"),
    ("@人物图", {"first_frame_url": "https://cdn.example/other.png"}, "video"),
    ("@人物图", {"image_references": REFERENCES}, "image"),
])
def test_reference_compiler_rejects_unbound_or_conflicting_inputs(content, extra, message_type):
    payload = ConversationSendMessageRequest(content=content, image_references=[REFERENCES[0]])
    with pytest.raises(AppException) as error:
        compile_image_references(content, extra, payload.image_references, message_type)
    assert error.value.status_code == 400


@pytest.mark.asyncio
async def test_video_provider_receives_ordered_reference_images():
    payload = ConversationSendMessageRequest(
        content="让 @人物图 在 @场景图 中奔跑", image_references=REFERENCES,
    )
    prompt, extra = compile_image_references(payload.content, {}, payload.image_references, "video")
    extra = await build_video_message_extra(extra)
    provider = apimart.build_video_generation_payload("seedance-2.5", prompt, extra)
    assert extra["generation_mode"] == "reference"
    assert provider["image_urls"] == [item["url"] for item in REFERENCES]
    assert provider["prompt"] == prompt


@pytest.mark.parametrize("message_type", ["image", "video"])
@pytest.mark.parametrize("content,expected", [
    ("参考这些图片，画一个海报。", "参考这些图片，画一个海报。"),
    ("参考@{场景图}的色调。", "参考@图片2的色调。"),
    ("将@{场景图}的衣服换到@{人物图}中。", "将@图片2的衣服换到@图片1中。"),
])
def test_selected_images_do_not_require_mentions_or_fixed_roles(message_type, content, expected):
    payload = ConversationSendMessageRequest(content=content, image_references=REFERENCES)
    prompt, extra = compile_image_references(content, {}, payload.image_references, message_type)
    assert prompt == expected
    assert extra["image_urls"] == [item["url"] for item in REFERENCES]


def test_overlapping_names_are_replaced_once_without_changing_bindings():
    references = [
        {"name": "图片10", "url": REFERENCES[0]["url"]},
        {"name": "图片1", "url": REFERENCES[1]["url"]},
    ]
    payload = ConversationSendMessageRequest(
        content="@图片1 和 @图片10，再用@{图片1}。", image_references=references,
    )
    prompt, extra = compile_image_references(payload.content, {}, payload.image_references, "image")
    assert prompt == "@图片2 和 @图片1，再用@图片2。"
    assert extra["image_urls"] == [item["url"] for item in references]


def test_requests_without_references_keep_existing_inputs():
    extra = {"image_urls": ["https://cdn.example/existing.png"]}
    assert compile_image_references("原提示词", extra, [], "image") == ("原提示词", extra)


@pytest.mark.parametrize("message_type", ["image", "video"])
@pytest.mark.parametrize("extra", [{}, {"image_urls": ["https://cdn.example/old.png"]}])
def test_editor_mentions_require_current_request_attachments(message_type, extra):
    with pytest.raises(AppException) as error:
        compile_image_references("继续修改@{图片1}。", extra, [], message_type)
    assert error.value.code == 40016


def test_native_provider_mentions_and_email_keep_legacy_behavior():
    extra = {"image_urls": ["https://cdn.example/current.png"]}
    content = "参考 @图片1，将 user@example.com 放在画面上。"
    assert compile_image_references(content, extra, [], "image") == (content, extra)


def test_image_mentions_do_not_rewrite_email_addresses():
    payload = ConversationSendMessageRequest(
        content="联系 user@example.com，使用 @example 。",
        image_references=[{**REFERENCES[0], "name": "example"}],
    )
    prompt, _ = compile_image_references(payload.content, {}, payload.image_references, "image")
    assert prompt.startswith("联系 user@example.com，使用 @图片1 。")
