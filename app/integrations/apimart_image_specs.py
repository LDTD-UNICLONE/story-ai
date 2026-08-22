import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Optional

from app.core.exceptions import AppException


_PIXEL_SIZE_PATTERN = re.compile(r"^([1-9]\d{1,4})x([1-9]\d{1,4})$")


@dataclass(frozen=True)
class ImageModelSpec:
    family: str
    ratios: FrozenSet[str]
    resolutions: FrozenSet[str] = frozenset()
    size_keywords: FrozenSet[str] = frozenset()
    min_images: int = 0
    max_images: int = 16
    min_outputs: int = 1
    max_outputs: int = 1
    allow_pixel_size: bool = False
    allowed_pixel_sizes: FrozenSet[str] = frozenset()
    allow_data_uri: bool = True
    data_uri_mimes: FrozenSet[str] = frozenset({"jpeg", "png", "gif", "webp"})
    ratio_field: str = "size"


_GPT_IMAGE_SPEC = ImageModelSpec(
    family="gpt_image",
    ratios=frozenset(
        {
            "auto",
            "1:1",
            "3:2",
            "2:3",
            "4:3",
            "3:4",
            "5:4",
            "4:5",
            "16:9",
            "9:16",
            "2:1",
            "1:2",
            "3:1",
            "1:3",
            "21:9",
            "9:21",
        }
    ),
    resolutions=frozenset({"1k", "2k", "4k"}),
    allow_pixel_size=True,
)

_SEEDREAM_5_SPEC = ImageModelSpec(
    family="seedream_5",
    ratios=frozenset(
        {"auto", "1:1", "4:3", "3:4", "16:9", "9:16", "3:2", "2:3", "2:1", "1:2", "21:9"}
    ),
    resolutions=frozenset({"1k", "1.5k", "2k"}),
    size_keywords=frozenset({"1k", "1.5k", "2k"}),
    max_images=10,
    allow_pixel_size=True,
    data_uri_mimes=frozenset({"jpeg", "png", "webp", "bmp", "tiff", "gif", "heic", "heif"}),
)

_SEEDREAM_45_SPEC = ImageModelSpec(
    family="seedream_45",
    ratios=frozenset(
        {"auto", "1:1", "4:3", "3:4", "16:9", "9:16", "3:2", "2:3", "2:1", "1:2", "21:9", "9:21"}
    ),
    resolutions=frozenset({"2k", "4k"}),
    max_images=14,
    max_outputs=15,
    data_uri_mimes=frozenset({"jpeg", "png"}),
)

_GEMINI_31_SPEC = ImageModelSpec(
    family="gemini_31",
    ratios=frozenset(
        {
            "auto",
            "1:1",
            "3:2",
            "2:3",
            "4:3",
            "3:4",
            "16:9",
            "9:16",
            "5:4",
            "4:5",
            "21:9",
            "1:4",
            "4:1",
            "1:8",
            "8:1",
        }
    ),
    resolutions=frozenset({"0.5k", "1k", "2k", "4k"}),
    max_images=14,
    data_uri_mimes=frozenset({"jpeg", "png", "webp"}),
)

_GEMINI_3_PRO_SPEC = ImageModelSpec(
    family="gemini_3_pro",
    ratios=frozenset(
        {"auto", "1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"}
    ),
    resolutions=frozenset({"1k", "2k", "4k"}),
    max_images=14,
    data_uri_mimes=frozenset({"jpeg", "png", "webp"}),
)

_QWEN_3_SPEC = ImageModelSpec(
    family="qwen_3",
    ratios=frozenset({"1:1", "4:3", "3:4", "16:9", "9:16", "3:2", "2:3"}),
    resolutions=frozenset({"1k", "2k"}),
    max_images=3,
    max_outputs=6,
    allow_pixel_size=True,
    data_uri_mimes=frozenset({"jpg", "jpeg", "png", "bmp", "tiff", "webp", "gif"}),
)

_GROK_15_SPEC = ImageModelSpec(
    family="grok_15",
    ratios=frozenset({"1:1", "16:9", "9:16", "3:2", "2:3"}),
    max_images=1,
    max_outputs=10,
    allow_data_uri=False,
)

_GROK_2_EXT_SPEC = ImageModelSpec(
    family="grok_2_ext",
    ratios=frozenset({"1:1", "2:3", "3:2", "3:4", "4:3", "9:16", "16:9"}),
    resolutions=frozenset({"quality"}),
    max_images=0,
    max_outputs=12,
    allow_pixel_size=True,
    allowed_pixel_sizes=frozenset({"1024x1024", "1024x1792", "1792x1024", "720x1280", "1280x720"}),
    allow_data_uri=False,
)

_GROK_OFFICIAL_SPEC = ImageModelSpec(
    family="grok_official",
    ratios=frozenset(
        {
            "auto",
            "1:1",
            "3:4",
            "4:3",
            "9:16",
            "16:9",
            "2:3",
            "3:2",
            "9:19.5",
            "19.5:9",
            "9:20",
            "20:9",
            "1:2",
            "2:1",
        }
    ),
    resolutions=frozenset({"1k", "2k"}),
    max_images=5,
    max_outputs=10,
    allow_data_uri=False,
    ratio_field="aspect_ratio",
)

_MODEL_SPECS = {
    "gpt-image-2": _GPT_IMAGE_SPEC,
    "gpt-image-2-ext": _GPT_IMAGE_SPEC,
    "seedream-5-0-pro": _SEEDREAM_5_SPEC,
    "seedream-5.0-pro": _SEEDREAM_5_SPEC,
    "seedream-4.5": _SEEDREAM_45_SPEC,
    "seedream-4-5": _SEEDREAM_45_SPEC,
    "gemini-3.1-flash-image-preview": _GEMINI_31_SPEC,
    "gemini-3.1-flash-image-preview-official": _GEMINI_31_SPEC,
    "nano-banana-2-ext": _GEMINI_31_SPEC,
    "nano-banana-2": _GEMINI_31_SPEC,
    "gemini-3-pro-image-preview": _GEMINI_3_PRO_SPEC,
    "gemini-3-pro-image-preview-official": _GEMINI_3_PRO_SPEC,
    "nano-banana-pro-ext": _GEMINI_3_PRO_SPEC,
    "nano-banana-pro": _GEMINI_3_PRO_SPEC,
    "qwen-image-3.0": _QWEN_3_SPEC,
    "qwen-image-3.0-pro": _QWEN_3_SPEC,
    "grok-imagine-1.5-apimart": _GROK_15_SPEC,
    "grok-imagine-1.5-ext": _GROK_15_SPEC,
    "grok-imagine-2.0-ext": _GROK_2_EXT_SPEC,
    "grok-imagine-2-0": _GROK_2_EXT_SPEC,
    "grok-imagine-image": _GROK_OFFICIAL_SPEC,
    "grok-imagine-image-quality": _GROK_OFFICIAL_SPEC,
}


def is_apimart_image_model(model: str) -> bool:
    return str(model or "").strip().lower() in _MODEL_SPECS


def build_image_payload(
    model: str,
    prompt: str,
    extra: Dict[str, Any],
    image_urls: List[str],
) -> Dict[str, Any]:
    model_id = str(model or "").strip()
    if not model_id:
        raise AppException("绘图 model 不能为空", code=40012, status_code=400)
    spec = image_model_spec(model_id)
    normalized_prompt = str(prompt or "").strip()
    layer_decomposition = _optional_bool(extra, "layer_decomposition")
    if not normalized_prompt and not (spec.family == "seedream_5" and layer_decomposition is True):
        raise AppException("绘图 prompt 不能为空", code=40012, status_code=400)

    _validate_reference_images(spec, model_id, image_urls)
    payload: Dict[str, Any] = {"model": model_id, "nsfw_check": True}
    if normalized_prompt:
        payload["prompt"] = normalized_prompt

    size = _first_value(extra, "size", "aspect_ratio", "ratio")
    if size is not None:
        payload[spec.ratio_field] = _normalize_size(size, spec)

    resolution = _first_value(extra, "resolution")
    if resolution is not None:
        payload["resolution"] = _normalize_resolution(resolution, spec)

    output_count = 1
    if extra.get("n") is not None:
        output_count = _bounded_int("n", extra["n"], spec.min_outputs, spec.max_outputs)
        payload["n"] = output_count

    if image_urls:
        payload["image_urls"] = image_urls

    _merge_family_fields(
        payload,
        spec,
        model_id,
        extra,
        image_urls,
        output_count,
        layer_decomposition,
    )
    return payload


def image_model_capabilities(model: str) -> Dict[str, Any]:
    spec = image_model_spec(model)
    max_reference_images = (
        3 if str(model or "").strip().lower() == "grok-imagine-image-quality" else spec.max_images
    )
    fields = [spec.ratio_field, "n"]
    if spec.max_images:
        fields.append("image_urls")
    if spec.resolutions:
        fields.append("resolution")
    family_fields = {
        "seedream_5": ["background"],
        "seedream_45": ["sequential_image_generation"],
        "gemini_31": ["web_search"],
        "qwen_3": ["negative_prompt"],
    }
    fields.extend(family_fields.get(spec.family, []))
    advanced_fields = {
        "seedream_5": ["optimize_prompt_options", "layer_decomposition"],
        "seedream_45": ["optimize_prompt_options"],
    }.get(spec.family, [])
    if _supports_official_fallback(model, spec):
        advanced_fields = [*advanced_fields, "official_fallback"]
    platform_parameters = {"nsfw_check": True}
    if spec.family in {"seedream_5", "seedream_45"}:
        platform_parameters["watermark"] = False
    modes = ["text_to_image"]
    if spec.max_images:
        modes.append("image_to_image")
    capabilities = {
        "provider": "apimart",
        "model_family": spec.family,
        "endpoint": "/v1/images/generations",
        "task_endpoint": "/v1/tasks/{task_id}",
        "modes": modes,
        "request_keys": fields,
        "advanced_request_keys": advanced_fields,
        "platform_parameters": platform_parameters,
        "ratios": sorted(spec.ratios),
        "pixel_sizes": sorted(spec.allowed_pixel_sizes),
        "resolutions": sorted(spec.resolutions),
        "max_reference_images": max_reference_images,
        "max_outputs": spec.max_outputs,
        "supports_async_task": True,
    }
    return capabilities


def merge_image_capabilities(model: str, saved: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    inferred = image_model_capabilities(model)
    if not saved:
        return inferred
    merged = deepcopy(inferred)
    for key, value in saved.items():
        if value is not None:
            merged[key] = value
    for key in (
        "endpoint",
        "max_outputs",
        "max_reference_images",
        "model_family",
        "modes",
        "pixel_sizes",
        "provider",
        "ratios",
        "request_keys",
        "resolutions",
        "supports_async_task",
        "task_endpoint",
        "advanced_request_keys",
        "platform_parameters",
    ):
        merged[key] = inferred[key]
    return merged


def requires_stable_response_header(model: str) -> bool:
    return image_model_spec(model).family in {"grok_2_ext", "grok_official"}


def preserves_reference_image_duplicates(model: str) -> bool:
    return image_model_spec(model).family == "grok_official"


def image_model_spec(model: str) -> ImageModelSpec:
    model_id = str(model or "").strip().lower()
    spec = _MODEL_SPECS.get(model_id)
    if spec is None:
        raise AppException(
            f"APIMart 图像模型 {model_id or 'unknown'} 尚未接入",
            code=40012,
            status_code=400,
        )
    return spec


def _merge_family_fields(
    payload: Dict[str, Any],
    spec: ImageModelSpec,
    model_id: str,
    extra: Dict[str, Any],
    image_urls: List[str],
    output_count: int,
    layer_decomposition: Optional[bool],
) -> None:
    family = spec.family
    if family in {"gpt_image", "gemini_31", "gemini_3_pro"}:
        _merge_official_fallback(payload, model_id, extra)
    if family == "seedream_5":
        _merge_seedream_5_fields(payload, extra, image_urls, layer_decomposition)
    elif family == "seedream_45":
        _merge_seedream_45_fields(payload, extra, image_urls, output_count)
    elif family == "gemini_31":
        _merge_gemini_31_fields(payload, extra)
    elif family == "qwen_3":
        _merge_qwen_3_fields(payload, extra, image_urls)
    elif family == "grok_official" and len(payload.get("prompt", "")) > 8000:
        raise AppException(
            "Grok 图像 prompt 不能超过 8000 个字符",
            code=40012,
            status_code=400,
        )


def _merge_official_fallback(payload: Dict[str, Any], model_id: str, extra: Dict[str, Any]) -> None:
    value = _optional_bool(extra, "official_fallback")
    if value is None:
        return
    if model_id.lower().endswith("-official") or model_id.lower() in {
        "nano-banana-2",
        "nano-banana-pro",
    }:
        raise AppException(
            "APIMart 官方图像模型不支持 official_fallback",
            code=40012,
            status_code=400,
        )
    payload["official_fallback"] = value


def _supports_official_fallback(model: str, spec: ImageModelSpec) -> bool:
    if spec.family not in {"gpt_image", "gemini_31", "gemini_3_pro"}:
        return False
    model_id = str(model or "").strip().lower()
    return not (model_id.endswith("-official") or model_id in {"nano-banana-2", "nano-banana-pro"})


def _merge_seedream_5_fields(
    payload: Dict[str, Any],
    extra: Dict[str, Any],
    image_urls: List[str],
    layer_decomposition: Optional[bool],
) -> None:
    background = _optional_enum(extra, "background", {"opaque", "transparent"})
    output_format = _first_value(extra, "output_format", "response_format")
    if background == "transparent" and output_format is None:
        output_format = "png"
    if output_format is not None:
        output_format = _enum("output_format", output_format, {"jpeg", "png"})
        payload["output_format"] = output_format
    if background is not None:
        if background == "transparent" and (len(image_urls) != 1 or output_format != "png"):
            raise AppException(
                "Seedream 5 透明背景需要恰好 1 张参考图并设置 output_format=png",
                code=40012,
                status_code=400,
            )
        payload["background"] = background
    if layer_decomposition is not None:
        if layer_decomposition:
            if len(image_urls) != 1:
                raise AppException(
                    "Seedream 5 图层拆分需要恰好 1 张参考图",
                    code=40012,
                    status_code=400,
                )
            size = payload.get("size", "auto")
            if size not in {"auto", "1k", "1.5k", "2k"}:
                raise AppException(
                    "Seedream 5 图层拆分 size 仅支持 auto、1k、1.5k、2k",
                    code=40012,
                    status_code=400,
                )
        payload["layer_decomposition"] = layer_decomposition
    optimize_mode = _optimize_prompt_mode(extra)
    if optimize_mode is not None:
        if optimize_mode != "standard":
            raise AppException(
                "Seedream 5 提示词优化仅支持 standard",
                code=40012,
                status_code=400,
            )
        payload["optimize_prompt_options"] = {"mode": optimize_mode}
    payload["watermark"] = False


def _merge_seedream_45_fields(
    payload: Dict[str, Any],
    extra: Dict[str, Any],
    image_urls: List[str],
    output_count: int,
) -> None:
    if output_count > 1 and not image_urls:
        raise AppException(
            "Seedream 4.5 纯文生图仅支持生成 1 张图片",
            code=40012,
            status_code=400,
        )
    if len(image_urls) + output_count > 15:
        raise AppException(
            "Seedream 4.5 参考图数量与生成数量之和不能超过 15",
            code=40012,
            status_code=400,
        )
    optimize_mode = _optimize_prompt_mode(extra)
    if optimize_mode is not None:
        payload["optimize_prompt_options"] = {
            "mode": _enum("optimize_prompt_options.mode", optimize_mode, {"standard", "fast"})
        }
    sequential = _optional_enum(extra, "sequential_image_generation", {"disabled", "auto"})
    if output_count > 1 and sequential == "disabled":
        raise AppException(
            "Seedream 4.5 生成多张图片时不能禁用组图模式",
            code=40012,
            status_code=400,
        )
    if output_count > 1 and sequential is None:
        sequential = "auto"
    if sequential is not None:
        if sequential == "auto" and not image_urls:
            raise AppException(
                "Seedream 4.5 组图生成需要至少 1 张参考图",
                code=40012,
                status_code=400,
            )
        payload["sequential_image_generation"] = sequential
    options = extra.get("sequential_image_generation_options")
    if options is not None:
        if not isinstance(options, dict):
            raise AppException(
                "sequential_image_generation_options 必须是对象",
                code=40012,
                status_code=400,
            )
        if sequential != "auto":
            raise AppException(
                "sequential_image_generation_options 需要启用组图模式",
                code=40012,
                status_code=400,
            )
        max_images = _bounded_int("max_images", options.get("max_images"), 1, 15)
        if len(image_urls) + max_images > 15:
            raise AppException(
                "Seedream 4.5 参考图数量与组图最大数量之和不能超过 15",
                code=40012,
                status_code=400,
            )
        payload["sequential_image_generation_options"] = {"max_images": max_images}
    payload["watermark"] = False


def _merge_gemini_31_fields(payload: Dict[str, Any], extra: Dict[str, Any]) -> None:
    web_search = _optional_bool(extra, "web_search")
    google_search = _optional_bool(extra, "google_search")
    google_image_search = _optional_bool(extra, "google_image_search")
    if web_search is not None:
        if google_search is None:
            google_search = web_search
        if google_image_search is None:
            google_image_search = web_search
    if google_image_search and not google_search:
        raise AppException(
            "google_image_search 需要同时启用 google_search",
            code=40012,
            status_code=400,
        )
    if google_search is not None:
        payload["google_search"] = google_search
    if google_image_search is not None:
        payload["google_image_search"] = google_image_search


def _merge_qwen_3_fields(
    payload: Dict[str, Any], extra: Dict[str, Any], image_urls: List[str]
) -> None:
    negative_prompt = extra.get("negative_prompt")
    if negative_prompt is not None:
        normalized = str(negative_prompt).strip()
        if not normalized:
            raise AppException("negative_prompt 不能为空", code=40012, status_code=400)
        payload["negative_prompt"] = normalized
    prompt_extend = _optional_bool(extra, "prompt_extend")
    if prompt_extend is not None:
        payload["prompt_extend"] = prompt_extend
    mode = extra.get("prompt_extend_mode")
    if mode is not None:
        normalized_mode = _enum("prompt_extend_mode", mode, {"direct", "agent"})
        if prompt_extend is not True:
            raise AppException(
                "prompt_extend_mode 需要同时设置 prompt_extend=true",
                code=40012,
                status_code=400,
            )
        if normalized_mode == "agent" and image_urls:
            raise AppException(
                "Qwen Image agent 提示词扩写仅支持文生图",
                code=40012,
                status_code=400,
            )
        payload["prompt_extend_mode"] = normalized_mode


def _validate_reference_images(spec: ImageModelSpec, model_id: str, image_urls: List[str]) -> None:
    max_images = spec.max_images
    if model_id.lower() == "grok-imagine-image-quality":
        max_images = 3
    if len(image_urls) < spec.min_images or len(image_urls) > max_images:
        raise AppException(
            f"{model_id} 参考图数量仅支持 {spec.min_images}-{max_images} 张",
            code=40012,
            status_code=400,
        )
    for url in image_urls:
        normalized = url.lower()
        if normalized.startswith(("http://", "https://")):
            continue
        mime_match = re.match(r"^data:image/([^;,]+);base64,", normalized)
        if (
            not spec.allow_data_uri
            or mime_match is None
            or mime_match.group(1) not in spec.data_uri_mimes
        ):
            supported = "、".join(sorted(spec.data_uri_mimes))
            message = (
                f"{model_id} 参考图仅支持公网 HTTP(S) URL"
                if not spec.allow_data_uri
                else f"{model_id} Base64 参考图仅支持 {supported} Data URI"
            )
            raise AppException(message, code=40012, status_code=400)
        if len(url) > 28 * 1024 * 1024:
            raise AppException(
                "Base64 参考图不能超过平台 20MB 限制",
                code=40012,
                status_code=400,
            )


def _normalize_size(value: Any, spec: ImageModelSpec) -> str:
    normalized = str(value or "").strip().lower().replace("×", "x")
    if normalized in spec.ratios or normalized in spec.size_keywords:
        return normalized
    ratio_alias = _ratio_alias(normalized)
    if ratio_alias in spec.ratios:
        return ratio_alias
    match = _PIXEL_SIZE_PATTERN.fullmatch(normalized)
    if match is None or not spec.allow_pixel_size:
        raise AppException(
            "APIMart 图像 size 不在当前模型支持范围内",
            code=40012,
            status_code=400,
        )
    width, height = int(match.group(1)), int(match.group(2))
    if spec.allowed_pixel_sizes and normalized not in spec.allowed_pixel_sizes:
        raise AppException(
            "APIMart 图像 size 不在当前模型支持范围内",
            code=40012,
            status_code=400,
        )
    if spec.family == "seedream_5":
        pixels = width * height
        ratio = width / height
        if not 921_600 <= pixels <= 4_624_220 or not 1 / 16 <= ratio <= 16:
            raise AppException(
                "Seedream 5 精确像素尺寸超出支持范围",
                code=40012,
                status_code=400,
            )
    elif spec.family == "qwen_3":
        ratio = width / height
        if not 512 <= width <= 2048 or not 512 <= height <= 2048 or not 1 / 8 <= ratio <= 8:
            raise AppException(
                "Qwen Image 像素尺寸需要单边 512-2048 且宽高比在 1:8-8:1 之间",
                code=40012,
                status_code=400,
            )
    return normalized


def _normalize_resolution(value: Any, spec: ImageModelSpec) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in spec.resolutions:
        supported = "、".join(sorted(spec.resolutions)) or "不支持"
        raise AppException(
            f"APIMart 当前图像模型 resolution 仅支持 {supported}",
            code=40012,
            status_code=400,
        )
    return normalized


def _ratio_alias(value: str) -> str:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)", value)
    return f"{match.group(1)}:{match.group(2)}" if match else ""


def _optimize_prompt_mode(extra: Dict[str, Any]) -> Optional[str]:
    value = extra.get("optimize_prompt_options")
    if value is not None:
        if not isinstance(value, dict):
            raise AppException("optimize_prompt_options 必须是对象", code=40012, status_code=400)
        value = value.get("mode")
    if value is None:
        value = extra.get("optimize_prompt_options.mode")
    return str(value).strip().lower() if value is not None else None


def _optional_bool(extra: Dict[str, Any], key: str) -> Optional[bool]:
    value = extra.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise AppException(f"{key} 必须是布尔值", code=40012, status_code=400)
    return value


def _optional_enum(extra: Dict[str, Any], key: str, allowed: set[str]) -> Optional[str]:
    value = extra.get(key)
    return _enum(key, value, allowed) if value is not None else None


def _enum(key: str, value: Any, allowed: set[str]) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in allowed:
        raise AppException(
            f"{key} 仅支持 {', '.join(sorted(allowed))}", code=40012, status_code=400
        )
    return normalized


def _bounded_int(key: str, value: Any, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AppException(f"{key} 必须是 {minimum}-{maximum} 的整数", code=40012, status_code=400)
    if value < minimum or value > maximum:
        raise AppException(f"{key} 必须是 {minimum}-{maximum} 的整数", code=40012, status_code=400)
    return value


def _first_value(extra: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = extra.get(key)
        if value not in (None, ""):
            return value
    return None
