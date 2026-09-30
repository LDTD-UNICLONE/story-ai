from types import SimpleNamespace

import httpx
import pytest
from openai import BadRequestError, InternalServerError

from app.core.exceptions import AppException
from app.integrations import apimart
from app.integrations.apimart_image_specs import image_model_capabilities
from app.services.models.catalog import resolve_ai_model_capabilities
from app.services.generation.runner import query_model_task, run_model


class _RawResponse:
    def __init__(self, payload=None, *, lines=None, content_type="application/json"):
        self.payload = payload
        self.lines = list(lines or [])
        self.headers = {"content-type": content_type}
        self.closed = False

    async def aread(self):
        return b""

    def json(self):
        return self.payload

    async def aiter_lines(self):
        for line in self.lines:
            yield line

    async def aclose(self):
        self.closed = True


class _ResponsesClient:
    def __init__(self, captured, payload):
        self.captured = captured
        self.payload = payload

    async def post(self, path, **kwargs):
        self.captured.update(path=path, **kwargs)
        return _RawResponse(self.payload)


class _StreamingClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def post(self, path, **kwargs):
        self.calls.append({"path": path, **kwargs})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _bad_request_error() -> BadRequestError:
    request = httpx.Request("POST", "https://api.apib.ai/v1/responses")
    response = httpx.Response(400, request=request)
    return BadRequestError("Responses unsupported", response=response, body={})


def _internal_server_error() -> InternalServerError:
    request = httpx.Request("POST", "https://api.apib.ai/v1/responses")
    response = httpx.Response(500, request=request)
    return InternalServerError("Internal Server Error", response=response, body={})


def test_apimart_chat_payload_keeps_backend_messages_and_supported_parameters() -> None:
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
    ]

    payload = apimart.build_chat_completion_payload(
        "gpt-5",
        "fallback",
        {
            "messages": messages,
            "temperature": 0.2,
            "callback_url": "https://example.com/ignored",
            "stream": True,
        },
    )

    assert payload == {
        "model": "gpt-5",
        "messages": messages,
        "stream": True,
        "temperature": 0.2,
    }


@pytest.mark.asyncio
async def test_apimart_background_text_uses_non_streaming_chat_endpoint(monkeypatch) -> None:
    captured = {}
    client = _ResponsesClient(
        captured,
        {"choices": [{"message": {"content": "background result"}}]},
    )

    async def get_client():
        return client

    monkeypatch.setattr(apimart, "_get_client", get_client)
    monkeypatch.setattr(apimart, "_base_url", lambda: "https://api.apib.ai/v1")

    payload = await apimart.create_chat_completion(
        "gpt-5",
        "analyze script",
        {"messages": [{"role": "user", "content": "analyze script"}]},
        stream=False,
        idempotency_key="background-task-123",
    )

    assert payload["choices"][0]["message"]["content"] == "background result"
    assert payload["provider_api"] == "chat_completions_nostream"
    assert payload["streamed"] is False
    assert captured["path"] == "https://api.apib.ai/api/v1/chat/completions"
    assert captured["body"]["stream"] is False
    assert captured["stream"] is False
    assert captured["options"] == {"idempotency_key": "background-task-123"}


@pytest.mark.asyncio
async def test_apimart_uses_streaming_responses_for_plain_text(monkeypatch) -> None:
    response = _RawResponse(
        lines=[
            'data: {"type":"response.output_text.delta","delta":"APIMart "}',
            'data: {"type":"response.output_text.delta","delta":"OK"}',
            'data: {"type":"response.completed","response":{"id":"resp-1"}}',
            "data: [DONE]",
        ],
        content_type="text/event-stream",
    )

    client = _StreamingClient([response])
    deltas = []

    async def get_client():
        return client

    async def on_text_delta(delta):
        deltas.append(delta)

    monkeypatch.setattr(apimart, "_get_client", get_client)

    payload = await apimart.create_chat_completion(
        "gpt-5",
        "hello",
        {"capability": "responses"},
        stream=True,
        idempotency_key="task-record-123",
        on_text_delta=on_text_delta,
    )

    assert payload["output_text"] == "APIMart OK"
    assert payload["provider_api"] == "responses"
    assert deltas == ["APIMart ", "OK"]
    assert client.calls[0]["path"] == "/responses"
    assert client.calls[0]["options"] == {"idempotency_key": "task-record-123"}
    assert client.calls[0]["body"]["stream"] is True
    assert client.calls[0]["stream"] is True
    assert response.closed is True


@pytest.mark.asyncio
async def test_apimart_plain_chat_uses_streaming_chat_endpoint_without_responses(
    monkeypatch,
) -> None:
    response = _RawResponse(
        lines=[
            'data: {"choices":[{"delta":{"content":"APIMart "}}]}',
            'data: {"choices":[{"delta":{"content":"OK"}}]}',
            "data: [DONE]",
        ],
        content_type="text/event-stream",
    )

    class Client(_StreamingClient):
        async def post(self, path, **kwargs):
            if path == "/responses":
                raise _internal_server_error()
            return await super().post(path, **kwargs)

    client = Client([response])

    async def get_client():
        return client

    monkeypatch.setattr(apimart, "_get_client", get_client)

    payload = await apimart.create_chat_completion(
        "gemini-3.1-pro-preview",
        "hello",
        {
            "capability": "chat",
            "messages": [{"role": "user", "content": "hello"}],
        },
        stream=True,
    )

    assert payload["choices"][0]["message"]["content"] == "APIMart OK"
    assert payload["provider_api"] == "chat_completions"
    assert [call["path"] for call in client.calls] == ["/chat/completions"]
    assert client.calls[0]["body"]["stream"] is True
    assert client.calls[0]["stream"] is True


def test_apimart_responses_payload_converts_text_and_image_messages() -> None:
    payload = apimart.build_responses_payload(
        "gpt-5.2-pro",
        "fallback",
        {
            "messages": [
                {"role": "assistant", "content": "请发图片"},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "分析构图"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://cdn.example/scene.webp"},
                        },
                    ],
                },
            ],
            "system_prompt": "你是视觉分析助手",
            "temperature": 0.3,
            "top_p": 0.9,
            "max_tokens": 1200,
            "tools": [{"type": "web_search"}],
            "stream": True,
        },
    )

    assert payload == {
        "model": "gpt-5.2-pro",
        "input": [
            {
                "role": "system",
                "content": [{"type": "input_text", "text": "你是视觉分析助手"}],
            },
            {
                "role": "assistant",
                "content": [{"type": "input_text", "text": "请发图片"}],
            },
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "分析构图"},
                    {
                        "type": "input_image",
                        "image_url": "https://cdn.example/scene.webp",
                    },
                ],
            },
        ],
        "stream": True,
        "temperature": 0.3,
        "top_p": 0.9,
        "max_tokens": 1200,
        "tools": [{"type": "web_search"}],
    }


def test_apimart_responses_adds_direct_uploaded_images_to_text_input() -> None:
    payload = apimart.build_responses_payload(
        "gpt-5.2-pro",
        "分析图片",
        {"uploaded_images": [{"url": "https://cdn.example/direct.png"}]},
    )

    assert payload["input"] == [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "分析图片"},
                {
                    "type": "input_image",
                    "image_url": "https://cdn.example/direct.png",
                },
            ],
        }
    ]


@pytest.mark.asyncio
async def test_apimart_responses_uses_sdk_and_unwraps_documented_data(monkeypatch) -> None:
    captured = {}
    client = _ResponsesClient(
        captured,
        {
            "code": 200,
            "data": {
                "id": "resp-123",
                "choices": [{"message": {"content": "图中是一座古城"}}],
            },
        },
    )

    async def get_client():
        return client

    monkeypatch.setattr(apimart, "_get_client", get_client)

    payload = await apimart.create_chat_completion(
        "gpt-5.2-pro",
        "分析图片",
        {
            "capability": "analyze_image",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "分析图片"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://cdn.example/scene.png"},
                        },
                    ],
                }
            ],
        },
        stream=True,
        idempotency_key="task-record-789",
    )

    assert payload["choices"][0]["message"]["content"] == "图中是一座古城"
    assert captured["path"] == "/responses"
    assert captured["cast_to"] is httpx.Response
    assert captured["options"] == {"idempotency_key": "task-record-789"}
    assert captured["body"]["stream"] is True
    assert captured["stream"] is True


@pytest.mark.asyncio
async def test_apimart_plain_text_falls_back_to_streaming_chat(monkeypatch) -> None:
    chat_response = _RawResponse(
        lines=[
            'data: {"choices":[{"delta":{"content":"回退"}}]}',
            'data: {"choices":[{"delta":{"content":"成功"}}]}',
            "data: [DONE]",
        ],
        content_type="text/event-stream",
    )
    client = _StreamingClient([_bad_request_error(), chat_response])

    async def get_client():
        return client

    monkeypatch.setattr(apimart, "_get_client", get_client)

    payload = await apimart.create_chat_completion(
        "gpt-5",
        "hello",
        {"capability": "responses"},
        stream=True,
    )

    assert payload["choices"][0]["message"]["content"] == "回退成功"
    assert payload["provider_api"] == "chat_completions_fallback"
    assert [call["path"] for call in client.calls] == [
        "/responses",
        "/chat/completions",
    ]
    assert client.calls[1]["body"]["stream"] is True


@pytest.mark.asyncio
async def test_apimart_partial_responses_stream_does_not_fall_back(monkeypatch) -> None:
    response = _RawResponse(
        lines=[
            'data: {"type":"response.output_text.delta","delta":"部分内容"}',
            "data: invalid-json",
        ],
        content_type="text/event-stream",
    )
    client = _StreamingClient([response])

    async def get_client():
        return client

    monkeypatch.setattr(apimart, "_get_client", get_client)

    with pytest.raises(AppException) as exc_info:
        await apimart.create_chat_completion(
            "gpt-5",
            "hello",
            {"capability": "responses"},
            stream=True,
        )

    assert exc_info.value.code == 50231
    assert [call["path"] for call in client.calls] == ["/responses"]
    assert response.closed is True


@pytest.mark.asyncio
async def test_apimart_image_request_does_not_fall_back_to_chat(monkeypatch) -> None:
    client = _StreamingClient([_bad_request_error()])

    async def get_client():
        return client

    monkeypatch.setattr(apimart, "_get_client", get_client)

    with pytest.raises(AppException) as exc_info:
        await apimart.create_chat_completion(
            "gpt-5",
            "分析图片",
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "分析图片"},
                            {
                                "type": "image_url",
                                "image_url": {"url": "https://cdn.example/image.png"},
                            },
                        ],
                    }
                ]
            },
            stream=True,
        )

    assert exc_info.value.status_code == 400
    assert [call["path"] for call in client.calls] == ["/responses"]


def test_apimart_responses_rejects_video_inputs() -> None:
    with pytest.raises(AppException) as exc_info:
        apimart.build_responses_payload(
            "gpt-5.2-pro",
            "分析视频",
            {"capability": "responses", "video_url": "https://cdn.example/demo.mp4"},
        )

    assert getattr(exc_info.value, "status_code", None) == 400


def test_apimart_image_payload_supports_generation_and_reference_images() -> None:
    payload = apimart.build_image_generation_payload(
        "gpt-image-2",
        " 把两张图融合成漫剧海报 ",
        {
            "aspect_ratio": "16:9",
            "resolution": "2K",
            "n": 1,
            "nsfw_check": False,
            "official_fallback": False,
            "images": ["https://cdn.example/character.png"],
            "reference_images": [
                {"url": "https://cdn.example/scene.png"},
                "https://cdn.example/character.png",
            ],
        },
    )

    assert payload == {
        "model": "gpt-image-2",
        "prompt": "把两张图融合成漫剧海报",
        "size": "16:9",
        "resolution": "2k",
        "n": 1,
        "nsfw_check": True,
        "official_fallback": False,
        "image_urls": [
            "https://cdn.example/character.png",
            "https://cdn.example/scene.png",
        ],
    }


@pytest.mark.parametrize("value", [0, 2, True, "invalid"])
def test_apimart_image_payload_rejects_unsupported_count(value) -> None:
    with pytest.raises(AppException) as exc_info:
        apimart.build_image_generation_payload(
            "gpt-image-2",
            "生成图片",
            {"n": value},
        )

    assert exc_info.value.status_code == 400


def test_apimart_image_payload_rejects_more_than_16_references() -> None:
    with pytest.raises(AppException) as exc_info:
        apimart.build_image_generation_payload(
            "gpt-image-2",
            "生成图片",
            {"image_urls": [f"https://cdn.example/{index}.png" for index in range(17)]},
        )

    assert exc_info.value.status_code == 400


def test_apimart_image_payload_rejects_models_without_a_contract() -> None:
    with pytest.raises(AppException) as exc_info:
        apimart.build_image_generation_payload(
            "unconfigured-image-model",
            "生成图片",
            {},
        )

    assert exc_info.value.code == 40012
    assert "尚未接入" in exc_info.value.message


def test_apimart_seedream_5_payload_supports_documented_parameters() -> None:
    payload = apimart.build_image_generation_payload(
        "seedream-5-0-pro",
        "保留主体并生成透明背景",
        {
            "size": "2x1",
            "resolution": "1.5K",
            "image_urls": ["https://cdn.example/subject.png"],
            "background": "transparent",
            "response_format": "png",
            "optimize_prompt_options": {"mode": "standard"},
            "watermark": True,
            "nsfw_check": False,
        },
    )

    assert payload == {
        "model": "seedream-5-0-pro",
        "prompt": "保留主体并生成透明背景",
        "size": "2:1",
        "resolution": "1.5k",
        "image_urls": ["https://cdn.example/subject.png"],
        "background": "transparent",
        "output_format": "png",
        "optimize_prompt_options": {"mode": "standard"},
        "watermark": False,
        "nsfw_check": True,
    }


def test_apimart_seedream_5_layer_decomposition_allows_empty_prompt() -> None:
    payload = apimart.build_image_generation_payload(
        "seedream-5.0-pro",
        "",
        {
            "size": "2K",
            "image_url": "https://cdn.example/poster.png",
            "layer_decomposition": True,
        },
    )

    assert "prompt" not in payload
    assert payload["size"] == "2k"
    assert payload["layer_decomposition"] is True


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"resolution": "4k"}, "resolution"),
        (
            {
                "image_url": "https://cdn.example/poster.png",
                "layer_decomposition": True,
                "size": "16:9",
            },
            "图层拆分",
        ),
    ],
)
def test_apimart_seedream_5_rejects_unsupported_combinations(extra, message) -> None:
    with pytest.raises(AppException) as exc_info:
        apimart.build_image_generation_payload("seedream-5-0-pro", "生成图片", extra)

    assert message in exc_info.value.message


def test_apimart_seedream_45_enables_multi_image_mode() -> None:
    payload = apimart.build_image_generation_payload(
        "Seedream-4.5",
        "生成连续动作组图",
        {
            "size": "9x21",
            "resolution": "4K",
            "n": 3,
            "image_urls": ["data:image/png;base64,AAAA"],
            "optimize_prompt_options.mode": "fast",
            "sequential_image_generation_options": {"max_images": 3},
            "watermark": True,
        },
    )

    assert payload["size"] == "9:21"
    assert payload["resolution"] == "4k"
    assert payload["n"] == 3
    assert payload["sequential_image_generation"] == "auto"
    assert payload["sequential_image_generation_options"] == {"max_images": 3}
    assert payload["optimize_prompt_options"] == {"mode": "fast"}
    assert payload["watermark"] is False
    assert payload["nsfw_check"] is True


def test_apimart_seedream_45_rejects_multiple_text_outputs() -> None:
    with pytest.raises(AppException) as exc_info:
        apimart.build_image_generation_payload(
            "seedream-4.5",
            "纯文生图",
            {"n": 2},
        )

    assert "纯文生图仅支持生成 1 张" in exc_info.value.message


def test_apimart_gemini_31_supports_search_and_extreme_ratio() -> None:
    payload = apimart.build_image_generation_payload(
        "gemini-3.1-flash-image-preview",
        "生成长幅信息图",
        {
            "size": "1x8",
            "resolution": "0.5K",
            "web_search": True,
            "official_fallback": True,
        },
    )

    assert payload["size"] == "1:8"
    assert payload["resolution"] == "0.5k"
    assert payload["google_search"] is True
    assert payload["google_image_search"] is True
    assert payload["official_fallback"] is True


def test_apimart_gemini_31_rejects_image_search_without_text_search() -> None:
    with pytest.raises(AppException) as exc_info:
        apimart.build_image_generation_payload(
            "nano-banana-2-ext",
            "生成图片",
            {"google_image_search": True},
        )

    assert "google_search" in exc_info.value.message


def test_apimart_gemini_official_model_rejects_fallback() -> None:
    with pytest.raises(AppException) as exc_info:
        apimart.build_image_generation_payload(
            "gemini-3-pro-image-preview-official",
            "生成图片",
            {"official_fallback": False},
        )

    assert "不支持 official_fallback" in exc_info.value.message


def test_apimart_qwen_3_payload_supports_generation_controls() -> None:
    payload = apimart.build_image_generation_payload(
        "qwen-image-3.0-pro",
        "生成菜单版式",
        {
            "size": "1600x900",
            "resolution": "2K",
            "n": 6,
            "negative_prompt": "水印",
            "prompt_extend": True,
            "prompt_extend_mode": "agent",
        },
    )

    assert payload == {
        "model": "qwen-image-3.0-pro",
        "prompt": "生成菜单版式",
        "size": "1600x900",
        "resolution": "2k",
        "n": 6,
        "negative_prompt": "水印",
        "prompt_extend": True,
        "prompt_extend_mode": "agent",
        "nsfw_check": True,
    }


def test_apimart_qwen_3_rejects_agent_rewrite_for_image_editing() -> None:
    with pytest.raises(AppException) as exc_info:
        apimart.build_image_generation_payload(
            "qwen-image-3.0",
            "编辑图片",
            {
                "image_url": "data:image/bmp;base64,AAAA",
                "prompt_extend": True,
                "prompt_extend_mode": "agent",
            },
        )

    assert "仅支持文生图" in exc_info.value.message


def test_apimart_grok_15_accepts_only_one_public_reference() -> None:
    payload = apimart.build_image_generation_payload(
        "grok-imagine-1.5-ext",
        "转换风格",
        {
            "size": "3:2",
            "n": 10,
            "image_url": "https://cdn.example/source.png",
        },
    )

    assert payload["n"] == 10
    assert payload["image_urls"] == ["https://cdn.example/source.png"]

    with pytest.raises(AppException) as exc_info:
        apimart.build_image_generation_payload(
            "grok-imagine-1.5-apimart",
            "转换风格",
            {"image_url": "data:image/png;base64,AAAA"},
        )
    assert "HTTP(S)" in exc_info.value.message


def test_apimart_grok_20_ext_uses_its_own_text_to_image_contract() -> None:
    payload = apimart.build_image_generation_payload(
        "grok-imagine-2.0-ext",
        "生成一组电影感场景图",
        {
            "size": "16:9",
            "resolution": "quality",
            "n": 12,
            "nsfw_check": True,
        },
    )

    assert payload == {
        "model": "grok-imagine-2.0-ext",
        "prompt": "生成一组电影感场景图",
        "size": "16:9",
        "resolution": "quality",
        "n": 12,
        "nsfw_check": True,
    }

    capabilities = image_model_capabilities("grok-imagine-2.0-ext")
    assert capabilities["model_family"] == "grok_2_ext"
    assert capabilities["modes"] == ["text_to_image"]
    assert capabilities["max_reference_images"] == 0
    assert capabilities["max_outputs"] == 12
    assert capabilities["resolutions"] == ["quality"]
    assert "image_urls" not in capabilities["request_keys"]


@pytest.mark.parametrize(
    "extra",
    [
        {"image_url": "https://cdn.example/source.png"},
        {"resolution": "2k"},
        {"size": "auto"},
        {"n": 13},
    ],
)
def test_apimart_grok_20_ext_rejects_unsupported_parameters(extra) -> None:
    with pytest.raises(AppException) as exc_info:
        apimart.build_image_generation_payload(
            "grok-imagine-2.0-ext",
            "生成图片",
            extra,
        )

    assert exc_info.value.status_code == 400


def test_apimart_grok_official_maps_ratio_and_preserves_duplicate_images() -> None:
    payload = apimart.build_image_generation_payload(
        "grok-imagine-image",
        "使用两次相同参考图",
        {
            "size": "19.5x9",
            "resolution": "2K",
            "n": 2,
            "image_urls": [
                "https://cdn.example/source.png",
                "https://cdn.example/source.png",
            ],
        },
    )

    assert "size" not in payload
    assert payload["aspect_ratio"] == "19.5:9"
    assert payload["image_urls"] == [
        "https://cdn.example/source.png",
        "https://cdn.example/source.png",
    ]


def test_apimart_image_capabilities_match_model_contract() -> None:
    capabilities = image_model_capabilities("grok-imagine-image-quality")

    assert capabilities["model_family"] == "grok_official"
    assert capabilities["max_reference_images"] == 3
    assert capabilities["max_outputs"] == 10
    assert "aspect_ratio" in capabilities["request_keys"]
    assert "size" not in capabilities["request_keys"]


def test_apimart_image_capabilities_separate_user_advanced_and_platform_parameters() -> None:
    seedream = image_model_capabilities("seedream-5-0-pro")

    assert set(seedream["request_keys"]) == {
        "size",
        "resolution",
        "n",
        "image_urls",
        "background",
    }
    assert set(seedream["advanced_request_keys"]) == {
        "optimize_prompt_options",
        "layer_decomposition",
    }
    assert seedream["platform_parameters"] == {
        "nsfw_check": True,
        "watermark": False,
    }

    gemini = image_model_capabilities("gemini-3.1-flash-image-preview")
    assert "web_search" in gemini["request_keys"]
    assert "google_search" not in gemini["request_keys"]
    assert "google_image_search" not in gemini["request_keys"]
    assert gemini["advanced_request_keys"] == ["official_fallback"]

    qwen = image_model_capabilities("qwen-image-3.0")
    assert "negative_prompt" in qwen["request_keys"]
    assert "prompt_extend" not in qwen["request_keys"]


def test_apimart_seedream_transparent_background_uses_png_automatically() -> None:
    payload = apimart.build_image_generation_payload(
        "seedream-5-0-pro",
        "生成透明背景素材",
        {
            "background": "transparent",
            "image_url": "https://cdn.example/subject.png",
        },
    )

    assert payload["background"] == "transparent"
    assert payload["output_format"] == "png"


def test_apimart_saved_image_capabilities_keep_documented_contract() -> None:
    model = SimpleNamespace(
        model_id="qwen-image-3.0",
        vendor="apimart",
        model_type="image",
        configuration={
            "request": {
                "capabilities": {
                    "display_group": "APIMart 图片",
                    "max_outputs": 99,
                }
            }
        },
    )

    capabilities = resolve_ai_model_capabilities(model)

    assert capabilities["display_group"] == "APIMart 图片"
    assert capabilities["max_outputs"] == 6
    assert capabilities["resolutions"] == ["1k", "2k"]


@pytest.mark.asyncio
async def test_apimart_image_generation_uses_async_task_endpoint(monkeypatch) -> None:
    captured = {}
    response = _RawResponse(
        {
            "code": 200,
            "data": [{"status": "submitted", "task_id": "task-image-123"}],
        }
    )
    client = _ResponsesClient(captured, response.payload)

    async def get_client():
        return client

    monkeypatch.setattr(apimart, "_get_client", get_client)

    payload = await apimart.create_image_generation(
        "gpt-image-2",
        "漫剧场景",
        {"ratio": "21:9", "resolution": "4k"},
        idempotency_key="task-record-image",
    )

    assert payload["data"][0]["task_id"] == "task-image-123"
    assert captured["path"] == "/images/generations"
    assert captured["cast_to"] is httpx.Response
    assert captured["options"] == {"idempotency_key": "task-record-image"}
    assert captured["body"] == {
        "model": "gpt-image-2",
        "prompt": "漫剧场景",
        "size": "21:9",
        "resolution": "4k",
        "nsfw_check": True,
    }


@pytest.mark.asyncio
async def test_apimart_grok_official_uses_stable_response_header(monkeypatch) -> None:
    captured = {}
    client = _ResponsesClient(
        captured,
        {
            "code": 202,
            "data": {"id": "task-grok-123", "status": "pending"},
        },
    )

    async def get_client():
        return client

    monkeypatch.setattr(apimart, "_get_client", get_client)

    payload = await apimart.create_image_generation(
        "grok-imagine-image-quality",
        "产品摄影",
        {"aspect_ratio": "4:3", "resolution": "2k"},
        idempotency_key="record-grok-123",
    )

    assert payload["data"]["id"] == "task-grok-123"
    assert captured["options"] == {
        "idempotency_key": "record-grok-123",
        "headers": {"X-APIMart-Response-Version": "2026-07-27"},
    }


@pytest.mark.asyncio
async def test_model_runner_routes_apimart_text_generation(monkeypatch) -> None:
    captured = {}

    async def create_chat_completion(model, prompt, extra, *, stream, idempotency_key=None):
        captured.update(
            model=model,
            prompt=prompt,
            extra=extra,
            stream=stream,
            idempotency_key=idempotency_key,
        )
        return {"choices": [{"message": {"content": "generated"}}]}

    monkeypatch.setattr(apimart, "create_chat_completion", create_chat_completion)
    model = SimpleNamespace(model_id="gpt-5", vendor="apimart", capabilities={})

    result = await run_model(
        model,
        "text",
        "hello",
        {},
        idempotency_key="task-record-456",
    )

    assert result.content == "generated"
    assert result.extra["provider_api"] == "chat_completions_nostream"
    assert result.extra["streamed"] is False
    assert captured == {
        "model": "gpt-5",
        "prompt": "hello",
        "extra": {},
        "stream": False,
        "idempotency_key": "task-record-456",
    }


@pytest.mark.asyncio
async def test_model_runner_marks_conversation_text_as_streaming(monkeypatch) -> None:
    captured = {}

    async def create_chat_completion(
        model,
        prompt,
        extra,
        *,
        stream,
        idempotency_key=None,
        on_text_delta=None,
    ):
        captured.update(stream=stream, on_text_delta=on_text_delta)
        return {"choices": [{"message": {"content": "generated"}}]}

    async def on_text_delta(_delta):
        return None

    monkeypatch.setattr(apimart, "create_chat_completion", create_chat_completion)
    model = SimpleNamespace(model_id="gpt-5", vendor="apimart", capabilities={})

    result = await run_model(
        model,
        "text",
        "hello",
        {},
        idempotency_key="conversation-task-456",
        on_text_delta=on_text_delta,
        text_stream=True,
    )

    assert result.content == "generated"
    assert captured == {"stream": True, "on_text_delta": on_text_delta}


@pytest.mark.asyncio
async def test_model_runner_extracts_standard_responses_output(monkeypatch) -> None:
    async def create_chat_completion(
        model,
        prompt,
        extra,
        *,
        stream=False,
        idempotency_key=None,
    ):
        return {
            "id": "resp-456",
            "object": "response",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "画面主体位于左侧。"}],
                }
            ],
        }

    monkeypatch.setattr(apimart, "create_chat_completion", create_chat_completion)
    model = SimpleNamespace(model_id="gpt-5.2-pro", vendor="apimart", capabilities={})

    result = await run_model(model, "text", "分析构图", {"capability": "responses"})

    assert result.content == "画面主体位于左侧。"


@pytest.mark.asyncio
async def test_model_runner_routes_apimart_image_generation(monkeypatch) -> None:
    captured = {}

    async def create_image_generation(model, prompt, extra, *, idempotency_key=None):
        captured.update(
            model=model,
            prompt=prompt,
            extra=extra,
            idempotency_key=idempotency_key,
        )
        return {
            "code": 200,
            "data": [{"status": "submitted", "task_id": "task-image-456"}],
        }

    monkeypatch.setattr(apimart, "create_image_generation", create_image_generation)
    model = SimpleNamespace(model_id="gpt-image-2", vendor="apimart", capabilities={})

    result = await run_model(
        model,
        "image",
        "生成分镜图",
        {"aspect_ratio": "16:9"},
        idempotency_key="record-456",
    )

    assert result.content == "图像生成任务已提交：task-image-456"
    assert result.extra["task_id"] == "task-image-456"
    assert result.extra["task_status"] == "submitted"
    assert captured == {
        "model": "gpt-image-2",
        "prompt": "生成分镜图",
        "extra": {"aspect_ratio": "16:9"},
        "idempotency_key": "record-456",
    }


@pytest.mark.asyncio
async def test_model_runner_accepts_grok_official_task_shape(monkeypatch) -> None:
    async def create_image_generation(model, prompt, extra, *, idempotency_key=None):
        return {
            "code": 202,
            "data": {
                "id": "task-grok-official",
                "object": "generation.task",
                "status": "pending",
            },
        }

    monkeypatch.setattr(apimart, "create_image_generation", create_image_generation)
    model = SimpleNamespace(
        model_id="grok-imagine-image",
        vendor="apimart",
        capabilities={},
    )

    result = await run_model(model, "image", "生成产品图", {})

    assert result.extra["task_id"] == "task-grok-official"
    assert result.extra["task_status"] == "pending"


@pytest.mark.asyncio
async def test_model_runner_normalizes_apimart_async_task_result(monkeypatch) -> None:
    async def query_generation_task(task_id):
        assert task_id == "task-123"
        return {
            "id": task_id,
            "status": "completed",
            "result": {"videos": [{"url": ["https://cdn.example/video.mp4"]}]},
        }

    monkeypatch.setattr(apimart, "query_generation_task", query_generation_task)
    model = SimpleNamespace(model_id="video-model", vendor="apimart", capabilities={})

    result = await query_model_task(model, "video", "task-123")

    assert result.content == "https://cdn.example/video.mp4"
    assert result.extra["task_id"] == "task-123"
    assert result.extra["task_status"] == "completed"
