from typing import Any, Dict, Optional, Set


RATIO_ALIASES = {
    "1": "1:1",
    "1:1": "1:1",
    "square": "1:1",
    "16:9": "16:9",
    "landscape": "16:9",
    "9:16": "9:16",
    "portrait": "9:16",
    "4:3": "4:3",
    "3:4": "3:4",
    "3:2": "3:2",
    "2:3": "2:3",
    "adaptive": "adaptive",
}

HIGH_PIXEL_SIZE_BY_RATIO = {
    "1:1": "1920x1920",
    "16:9": "2560x1440",
    "9:16": "1440x2560",
    "4:3": "2304x1728",
    "3:4": "1728x2304",
    "3:2": "2352x1568",
    "2:3": "1568x2352",
}

VIDEO_SIZE_BY_RATIO = {
    "1:1": "1024x1024",
    "16:9": "1280x720",
    "9:16": "720x1280",
    "4:3": "960x720",
    "3:4": "720x960",
}

IMAGE_ASPECT_RATIO_MODELS = ("flux", "recraft", "ideogram")


def normalize_ratio(value: Any) -> Optional[str]:
    if value is None:
        return None
    ratio = str(value).strip().lower().replace("*", ":").replace("x", ":")
    return RATIO_ALIASES.get(ratio)


def adapt_image_dimensions(model: str, payload: Dict[str, Any], extra: Dict[str, Any]) -> None:
    ratio = normalize_ratio(extra.get("aspect_ratio") or extra.get("ratio"))
    if not ratio:
        return
    if "size" in payload:
        return

    if _image_model_prefers_aspect_ratio(model):
        payload["aspect_ratio"] = ratio
        return

    size = HIGH_PIXEL_SIZE_BY_RATIO.get(ratio)
    if size:
        payload["size"] = size
        payload.pop("aspect_ratio", None)


def adapt_video_dimensions(
    payload: Dict[str, Any],
    extra: Dict[str, Any],
    allowed_keys: Set[str],
) -> None:
    ratio = normalize_ratio(extra.get("aspect_ratio") or extra.get("ratio"))
    if not ratio:
        return

    if "aspect_ratio" in allowed_keys:
        payload["aspect_ratio"] = ratio
        return

    if "size" in allowed_keys and "size" not in payload:
        size = VIDEO_SIZE_BY_RATIO.get(ratio)
        if size:
            payload["size"] = size


def _image_model_prefers_aspect_ratio(model: str) -> bool:
    normalized = model.lower()
    return any(token in normalized for token in IMAGE_ASPECT_RATIO_MODELS)
