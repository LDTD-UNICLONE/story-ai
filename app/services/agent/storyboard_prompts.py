import re
from typing import Any, Dict, List


ASSET_TOKEN_PATTERN = re.compile(r"\{\{asset:([a-z][a-z0-9_]{0,63})\}\}")


def asset_token(binding_key: str) -> str:
    return f"{{{{asset:{binding_key}}}}}"


def asset_token_keys(value: str) -> List[str]:
    return list(dict.fromkeys(ASSET_TOKEN_PATTERN.findall(value)))


def tokenize_asset_text(value: str, name_to_key: Dict[str, str]) -> str:
    result = value
    for name in sorted(name_to_key, key=len, reverse=True):
        normalized = name.strip()
        if normalized:
            result = result.replace(normalized, asset_token(name_to_key[name]))
    return result


def render_asset_tokens(value: str, labels: Dict[str, str]) -> str:
    return ASSET_TOKEN_PATTERN.sub(
        lambda match: labels.get(match.group(1), ""),
        value,
    )


def tokenize_storyboard_shots(
    shots: List[Dict[str, Any]],
    name_to_key: Dict[str, str],
) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for shot in shots:
        tokenized = _map_shot_strings(shot, name_to_key, tokenize_asset_text)
        tokenized["character_binding_keys"] = _shot_binding_keys(
            shot.get("character_binding_keys"),
            shot.get("characters") or [],
            name_to_key,
        )
        scene_keys = _shot_binding_keys(
            [shot.get("scene_binding_key")] if shot.get("scene_binding_key") else [],
            [shot.get("scene_name")] if shot.get("scene_name") else [],
            name_to_key,
        )
        tokenized["scene_binding_key"] = scene_keys[0] if scene_keys else None
        tokenized["prop_binding_keys"] = _shot_binding_keys(
            shot.get("prop_binding_keys"),
            shot.get("props") or [],
            name_to_key,
        )
        result.append(tokenized)
    return result


def render_storyboard_shots(
    shots: List[Dict[str, Any]],
    labels: Dict[str, str],
) -> List[Dict[str, Any]]:
    result = [_map_shot_strings(shot, labels, render_asset_tokens) for shot in shots]
    for shot in result:
        shot["character_binding_keys"] = [
            key for key in shot.get("character_binding_keys") or [] if key in labels
        ]
        scene_key = shot.get("scene_binding_key")
        shot["scene_binding_key"] = scene_key if scene_key in labels else None
        shot["prop_binding_keys"] = [
            key for key in shot.get("prop_binding_keys") or [] if key in labels
        ]
    return result


def _map_shot_strings(
    shot: Dict[str, Any],
    mapping: Dict[str, str],
    mapper,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in shot.items():
        if isinstance(value, str):
            result[key] = mapper(value, mapping)
        elif isinstance(value, list):
            result[key] = [
                mapper(item, mapping) if isinstance(item, str) else item for item in value
            ]
        else:
            result[key] = value
    return result


def _shot_binding_keys(
    explicit_keys: Any,
    names: Any,
    name_to_key: Dict[str, str],
) -> List[str]:
    values: List[str] = []
    for raw in explicit_keys or []:
        key = str(raw or "").strip()
        if key and key not in values:
            values.append(key)
    for raw in names or []:
        name = str(raw or "").strip()
        key = name_to_key.get(name)
        if key and key not in values:
            values.append(key)
        for token_key in ASSET_TOKEN_PATTERN.findall(name):
            if token_key not in values:
                values.append(token_key)
    return values
