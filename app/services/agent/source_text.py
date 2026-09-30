import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.core.exceptions import AppException
from app.models.agent_story_bible import AGENT_ASSET_VARIANT_TYPES


@dataclass(frozen=True)
class SourceTextChunk:
    index: int
    start_offset: int
    end_offset: int
    content: str
    content_hash: str


def split_source_text(content: str, max_characters: int) -> List[SourceTextChunk]:
    if max_characters <= 0:
        raise ValueError("max_characters must be greater than zero")
    if not content:
        return []

    chunks: List[SourceTextChunk] = []
    start = 0
    while start < len(content):
        hard_end = min(start + max_characters, len(content))
        end = hard_end
        if hard_end < len(content):
            end = _natural_break_offset(content, start, hard_end)
        chunk_content = content[start:end]
        chunks.append(
            SourceTextChunk(
                index=len(chunks),
                start_offset=start,
                end_offset=end,
                content=chunk_content,
                content_hash=hashlib.sha256(chunk_content.encode("utf-8")).hexdigest(),
            )
        )
        start = end
    return chunks


def parse_agent_json_object(content: str) -> Dict[str, Any]:
    normalized = (content or "").strip()
    if normalized.startswith("```") and normalized.endswith("```"):
        first_newline = normalized.find("\n")
        normalized = normalized[first_newline + 1 : -3].strip() if first_newline >= 0 else ""

    try:
        payload = json.loads(normalized)
    except json.JSONDecodeError:
        payload = _decode_embedded_json_object(normalized)
    if not isinstance(payload, dict):
        raise AppException("模型未返回有效的 JSON 对象", code=50241, status_code=502)
    return payload


def validate_agent_stage_output(
    stage: str,
    payload: Dict[str, Any],
    *,
    source_text: Optional[str] = None,
    source_start: int = 0,
) -> Dict[str, Any]:
    source_end = None
    if source_text is not None:
        source_end = source_start + len(source_text)
        payload = _ground_asset_evidence(payload, source_text, source_start)
    if stage == "chunk_analysis":
        return _validate_chunk_output(payload, source_start, source_end)
    if stage == "global_merge":
        return _validate_global_output(payload)
    if stage == "asset_analysis":
        return _validate_asset_output(payload, source_start, source_end)
    if stage == "episode_planning":
        if source_text is not None:
            payload = _ground_episode_ranges(payload, source_text, source_start)
        return _validate_episode_plan(payload)
    raise AppException("未知的整剧文本任务类型", code=50043, status_code=500)


def _natural_break_offset(content: str, start: int, hard_end: int) -> int:
    minimum = start + max(1, (hard_end - start) // 2)
    candidates = []
    for separator in ("\n\n", "\n", "。", "！", "？", ";", "；"):
        position = content.rfind(separator, minimum, hard_end)
        if position >= minimum:
            candidates.append(position + len(separator))
    return max(candidates, default=hard_end)


def _decode_embedded_json_object(content: str) -> Any:
    decoder = json.JSONDecoder()
    for index, character in enumerate(content):
        if character != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(content[index:])
            return payload
        except json.JSONDecodeError:
            continue
    raise AppException("模型未返回有效的 JSON 对象", code=50241, status_code=502)


def _validate_chunk_output(
    payload: Dict[str, Any],
    source_start: int = 0,
    source_end: Optional[int] = None,
) -> Dict[str, Any]:
    summary = str(payload.get("summary") or "").strip()
    if not summary:
        raise AppException("分块解析结果缺少 summary", code=50242, status_code=502)
    return {
        **payload,
        "summary": summary,
        "events": _list_value(payload, "events"),
        "characters": _validated_asset_list(
            payload, "characters", 50242, source_start, source_end
        ),
        "character_variants": _validated_variant_list(
            payload, "character_variants", "character", 50242, source_start, source_end
        ),
        "scenes": _validated_asset_list(
            payload, "scenes", 50242, source_start, source_end
        ),
        "scene_variants": _validated_variant_list(
            payload, "scene_variants", "scene", 50242, source_start, source_end
        ),
        "props": _validated_asset_list(
            payload, "props", 50242, source_start, source_end
        ),
        "prop_variants": _validated_variant_list(
            payload, "prop_variants", "prop", 50242, source_start, source_end
        ),
        "timeline": _list_value(payload, "timeline"),
        "unresolved": _list_value(payload, "unresolved"),
    }


def _validate_global_output(payload: Dict[str, Any]) -> Dict[str, Any]:
    story_summary = str(payload.get("story_summary") or "").strip()
    if not story_summary:
        raise AppException("全剧合并结果缺少 story_summary", code=50243, status_code=502)
    return {
        **payload,
        "story_summary": story_summary,
        "characters": _list_value(payload, "characters"),
        "character_variants": _validated_variant_list(
            payload, "character_variants", "character", 50243
        ),
        "scenes": _list_value(payload, "scenes"),
        "scene_variants": _validated_variant_list(
            payload, "scene_variants", "scene", 50243
        ),
        "props": _list_value(payload, "props"),
        "prop_variants": _validated_variant_list(
            payload, "prop_variants", "prop", 50243
        ),
        "timeline": _list_value(payload, "timeline"),
        "continuity_rules": _list_value(payload, "continuity_rules"),
    }


def _validate_asset_output(
    payload: Dict[str, Any],
    source_start: int = 0,
    source_end: Optional[int] = None,
) -> Dict[str, Any]:
    for key in (
        "characters",
        "character_variants",
        "scenes",
        "scene_variants",
        "props",
        "prop_variants",
    ):
        _required_list(payload, key, 50245)
    return {
        **payload,
        "characters": _validated_asset_list(
            payload,
            "characters",
            50245,
            source_start,
            source_end,
            require_design=True,
        ),
        "character_variants": _validated_variant_list(
            payload,
            "character_variants",
            "character",
            50245,
            source_start,
            source_end,
            require_design=True,
        ),
        "scenes": _validated_asset_list(
            payload,
            "scenes",
            50245,
            source_start,
            source_end,
            require_design=True,
        ),
        "scene_variants": _validated_variant_list(
            payload,
            "scene_variants",
            "scene",
            50245,
            source_start,
            source_end,
            require_design=True,
        ),
        "props": _validated_asset_list(
            payload,
            "props",
            50245,
            source_start,
            source_end,
            require_design=True,
        ),
        "prop_variants": _validated_variant_list(
            payload,
            "prop_variants",
            "prop",
            50245,
            source_start,
            source_end,
            require_design=True,
        ),
    }


def _validate_episode_plan(payload: Dict[str, Any]) -> Dict[str, Any]:
    episodes = _list_value(payload, "episodes")
    if not episodes:
        raise AppException("分集规划结果缺少 episodes", code=50244, status_code=502)
    normalized_episodes = []
    previous_source_end = None
    for index, episode in enumerate(episodes, start=1):
        if not isinstance(episode, dict):
            raise AppException("分集规划中的 episode 必须是对象", code=50244, status_code=502)
        title = str(episode.get("title") or "").strip()
        if not title:
            raise AppException("分集规划中的 episode 缺少 title", code=50244, status_code=502)
        content = str(episode.get("content") or "").strip()
        if not content:
            raise AppException("分集规划中的 episode 缺少 content", code=50244, status_code=502)
        normalized = {
            **episode,
            "episode_number": index,
            "title": title,
            "content": content,
            "ending_hook": str(episode.get("ending_hook") or episode.get("hook") or "").strip(),
            "characters": _list_value(episode, "characters"),
            "character_variants": _list_value(episode, "character_variants"),
            "scenes": _list_value(episode, "scenes"),
            "scene_variants": _list_value(episode, "scene_variants"),
            "props": _list_value(episode, "props"),
            "prop_variants": _list_value(episode, "prop_variants"),
            "continuity_notes": _list_value(episode, "continuity_notes"),
        }
        required_text = ("opening_hook", "goal", "conflict", "climax", "ending_hook")
        if any(not str(normalized.get(key) or "").strip() for key in required_text):
            raise AppException("分集规划缺少钩子、目标、冲突或高潮", code=50244, status_code=502)
        if not _has_source_reference(normalized):
            raise AppException("分集规划缺少原文字符范围或文本块 ID", code=50244, status_code=502)
        source_range = _source_range(normalized)
        if source_range is not None:
            if previous_source_end is not None and source_range[0] != previous_source_end:
                raise AppException("分集规划原文范围必须连续且不能重叠", code=50244, status_code=502)
            previous_source_end = source_range[1]
        else:
            previous_source_end = None
        normalized_episodes.append(normalized)
    return {**payload, "episodes": normalized_episodes}


def _list_value(payload: Dict[str, Any], key: str) -> list:
    value = payload.get(key)
    return value if isinstance(value, list) else []


def _required_list(payload: Dict[str, Any], key: str, error_code: int) -> list:
    value = payload.get(key)
    if not isinstance(value, list):
        raise AppException(f"资产分析结果缺少 {key} 数组", code=error_code, status_code=502)
    return value


def _validated_asset_list(
    payload: Dict[str, Any],
    key: str,
    error_code: int,
    source_start: int = 0,
    source_end: Optional[int] = None,
    *,
    require_design: bool = False,
) -> List[Dict[str, Any]]:
    result = []
    for raw in _required_list(payload, key, error_code):
        data = raw if isinstance(raw, dict) else {"name": raw}
        name = str(data.get("name") or data.get("canonical_name") or "").strip()
        if not name:
            raise AppException("基础资产缺少名称", code=error_code, status_code=502)
        evidence = data.get("source_evidence")
        evidence_valid = (
            isinstance(evidence, list)
            and bool(evidence)
            and all(
                _valid_source_range(
                    item,
                    source_start=source_start,
                    source_end=source_end,
                )
                for item in evidence
            )
        )
        if not evidence_valid and not _valid_source_range(
            data,
            source_start=source_start,
            source_end=source_end,
        ):
            raise AppException("基础资产缺少原文证据", code=error_code, status_code=502)
        if require_design:
            source_facts = data.get("source_facts")
            design_spec = data.get("design_spec")
            identity_anchors = (
                design_spec.get("identity_anchors")
                if isinstance(design_spec, dict)
                else None
            )
            if (
                not isinstance(source_facts, dict)
                or not source_facts
                or not isinstance(design_spec, dict)
                or not design_spec
                or not isinstance(identity_anchors, list)
                or not any(str(item).strip() for item in identity_anchors)
            ):
                raise AppException(
                    "基础资产缺少剧情事实、视觉设计或身份固定特征",
                    code=error_code,
                    status_code=502,
                )
        result.append({**data, "name": name})
    return result


def _validated_variant_list(
    payload: Dict[str, Any],
    key: str,
    asset_type: str,
    error_code: int,
    source_start: int = 0,
    source_end: Optional[int] = None,
    *,
    require_design: bool = False,
) -> List[Dict[str, Any]]:
    result = []
    for raw in _list_value(payload, key):
        if not isinstance(raw, dict):
            raise AppException("资产变体必须是对象", code=error_code, status_code=502)
        base_name = str(raw.get("base_name") or raw.get("asset_name") or "").strip()
        name = str(raw.get("name") or raw.get("variant_name") or "").strip()
        variant_type = str(raw.get("variant_type") or raw.get("type") or "").strip().lower()
        description = str(raw.get("description") or "").strip()
        trigger_reason = str(raw.get("trigger_reason") or "").strip()
        if not all((base_name, name, variant_type, description, trigger_reason)):
            raise AppException(
                "资产变体缺少基础资产、名称、类型、描述或触发原因",
                code=error_code,
                status_code=502,
            )
        if variant_type not in AGENT_ASSET_VARIANT_TYPES[asset_type]:
            raise AppException(
                f"{asset_type} 资产变体类型无效",
                code=error_code,
                status_code=502,
            )
        evidence = raw.get("source_evidence")
        if isinstance(evidence, list) and evidence:
            if any(
                not _valid_source_range(
                    item,
                    source_start=source_start,
                    source_end=source_end,
                )
                for item in evidence
            ):
                raise AppException("资产变体原文证据无效", code=error_code, status_code=502)
        elif not _valid_source_range(
            raw,
            source_start=source_start,
            source_end=source_end,
        ):
            raise AppException("资产变体缺少原文证据", code=error_code, status_code=502)
        state_scope = str(raw.get("state_scope") or "episode_only").strip().lower()
        if require_design:
            visual_delta = raw.get("visual_delta")
            preserve_anchors = raw.get("preserve_anchors")
            delta_groups = (
                [visual_delta.get(key) for key in ("add", "replace", "remove")]
                if isinstance(visual_delta, dict)
                else []
            )
            delta_values = [
                value
                for group in delta_groups
                if isinstance(group, list)
                for value in group
                if str(value).strip()
            ]
            if (
                len(delta_groups) != 3
                or any(not isinstance(group, list) for group in delta_groups)
                or not delta_values
                or not isinstance(preserve_anchors, list)
                or not any(str(item).strip() for item in preserve_anchors)
                or state_scope
                not in {"episode_only", "from_episode_until_changed", "recurring"}
            ):
                raise AppException(
                    "资产变体缺少可见变化、基础特征约束或有效状态范围",
                    code=error_code,
                    status_code=502,
                )
        result.append(
            {
                **raw,
                "base_name": base_name,
                "name": name,
                "variant_type": variant_type,
                "description": description,
                "trigger_reason": trigger_reason,
                "state_scope": state_scope,
            }
        )
    return result


def _valid_source_range(
    value: Any,
    *,
    source_start: int = 0,
    source_end: Optional[int] = None,
) -> bool:
    if not isinstance(value, dict):
        return False
    try:
        start = int(value.get("source_start"))
        end = int(value.get("source_end"))
    except (TypeError, ValueError):
        return False
    return end > start >= source_start and (source_end is None or end <= source_end)


def _ground_asset_evidence(
    payload: Dict[str, Any],
    source_text: str,
    source_start: int,
) -> Dict[str, Any]:
    grounded = deepcopy(payload)
    for key in (
        "characters",
        "character_variants",
        "scenes",
        "scene_variants",
        "props",
        "prop_variants",
    ):
        values = grounded.get(key)
        if not isinstance(values, list):
            continue
        for value in values:
            if not isinstance(value, dict):
                continue
            evidence = value.get("source_evidence")
            if isinstance(evidence, list) and evidence:
                for item in evidence:
                    if isinstance(item, dict):
                        _ground_source_range(item, source_text, source_start)
            else:
                _ground_source_range(value, source_text, source_start)
    return grounded


def _ground_source_range(
    value: Dict[str, Any],
    source_text: str,
    source_start: int,
) -> None:
    quote = str(value.get("source_quote") or "").strip()
    if not quote:
        value["source_start"] = -1
        value["source_end"] = -1
        return
    positions = []
    position = source_text.find(quote)
    while position >= 0:
        positions.append(position)
        position = source_text.find(quote, position + 1)
    if not positions:
        value["source_start"] = -1
        value["source_end"] = -1
        return
    try:
        expected_start = int(value.get("source_start")) - source_start
    except (TypeError, ValueError):
        expected_start = positions[0]
    matched_start = min(positions, key=lambda item: abs(item - expected_start))
    value["source_start"] = source_start + matched_start
    value["source_end"] = source_start + matched_start + len(quote)


def _ground_episode_ranges(
    payload: Dict[str, Any],
    source_text: str,
    source_start: int,
) -> Dict[str, Any]:
    grounded = deepcopy(payload)
    episodes = grounded.get("episodes")
    if not isinstance(episodes, list):
        return grounded
    cursor = 0
    for index, episode in enumerate(episodes):
        if not isinstance(episode, dict):
            continue
        quote = str(episode.get("source_end_quote") or "").strip()
        matched_start = source_text.find(quote, cursor) if quote else -1
        if matched_start < 0:
            episode["source_start"] = -1
            episode["source_end"] = -1
            continue
        matched_end = matched_start + len(quote)
        episode["source_start"] = source_start + cursor
        episode["source_end"] = source_start + (
            len(source_text) if index == len(episodes) - 1 else matched_end
        )
        cursor = matched_end
    return grounded


def _has_source_reference(episode: Dict[str, Any]) -> bool:
    block_ids = episode.get("source_block_ids")
    if isinstance(block_ids, list) and block_ids:
        return True
    try:
        return int(episode.get("source_end")) > int(episode.get("source_start")) >= 0
    except (TypeError, ValueError):
        return False


def _source_range(value: Dict[str, Any]) -> Any:
    try:
        start = int(value.get("source_start"))
        end = int(value.get("source_end"))
    except (TypeError, ValueError):
        return None
    return (start, end) if end > start >= 0 else None
