"""分镜结果解析与内容规范化；兼容普通项目及 Agent 镜头组，不访问数据库或队列。"""

import json
import math
import re
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from app.core.exceptions import AppException


def _parse_uuid(value: Any) -> Optional[UUID]:
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def parse_storyboard_stage_items(content: str, generation_type: str) -> List[Dict[str, Any]]:
    payload = _parse_json_payload(content)
    if generation_type == "storyboard_refinement":
        items = _extract_items_by_keys(
            payload,
            (
                "storyboard_execution_item",
                "storyboard_execution_items",
                "item",
                "items",
                "分镜细化",
                "分镜细化列表",
            ),
        )
        return [
            _normalize_storyboard_refinement_item(item, index)
            for index, item in enumerate(items or [], start=1)
            if isinstance(item, dict)
        ]
    if _is_storyboard_image_prompt_generation(generation_type):
        items = _extract_items_by_keys(
            payload,
            (
                "image_prompt_item",
                "image_prompt_items",
                "item",
                "items",
            ),
        )
        return [
            _normalize_storyboard_image_prompt_item(item, index)
            for index, item in enumerate(items or [], start=1)
            if isinstance(item, dict)
        ]
    if generation_type == "storyboard_prompt_generation":
        items = _extract_items_by_keys(
            payload,
            (
                "storyboard_prompt_item",
                "storyboard_prompt_items",
                "item",
                "items",
                "视频提示词",
                "视频提示词列表",
            ),
        )
        return [
            _normalize_storyboard_prompt_item(item, index)
            for index, item in enumerate(items or [], start=1)
            if isinstance(item, dict)
        ]
    return parse_storyboard_items(content)


def _extract_items_by_keys(payload: Any, keys: Tuple[str, ...]) -> Optional[List[Any]]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return None
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict) and _looks_like_storyboard_item(value):
            return [value]
    return _extract_storyboard_items(payload)


def _normalize_storyboard_refinement_item(item: Dict[str, Any], index: int) -> Dict[str, Any]:
    return {
        **item,
        "shot_number": _first_value(item, "shot_number", "分镜序号") or index,
        "title": _first_value(item, "title", "标题") or f"分镜{index}",
        "source_content": _first_value(item, "source_content", "原文") or "",
        "scene_name": _first_value(item, "scene_name", "场景名称") or "",
        "scene_state": _first_value(item, "scene_state", "场景状态") or "",
        "characters": _first_value(item, "characters", "人物") or [],
        "props": _first_value(item, "props", "道具") or [],
        "action": _first_value(item, "action", "动作") or "",
        "shot_size": _first_value(item, "shot_size", "景别") or "",
        "camera_angle": _first_value(item, "camera_angle", "拍摄角度") or "",
        "camera_movement": _first_value(item, "camera_movement", "运镜") or "",
        "screen_execution": _first_value(item, "screen_execution", "画面执行") or "",
        "character_action": _first_value(item, "character_action", "角色动作") or "",
        "character_expression": _first_value(item, "character_expression", "角色表情") or "",
        "dialogue": _first_value(item, "dialogue", "台词") or "",
        "sound_effect": _first_value(item, "sound_effect", "音效") or "",
        "atmosphere": _first_value(item, "atmosphere", "氛围参考", "画面氛围") or "",
        "duration_suggestion": _first_value(item, "duration_suggestion", "时长建议") or "",
        "production_focus": _first_value(item, "production_focus", "制作重点", "制作重点提示词")
        or "",
        "negative_prompt": _first_value(item, "negative_prompt", "负面规避词", "负面规避") or "",
        "ending_frame": _first_value(item, "ending_frame", "结尾画面", "收束画面") or "",
    }


def _normalize_storyboard_prompt_item(item: Dict[str, Any], index: int) -> Dict[str, Any]:
    return {
        **item,
        "shot_number": _first_value(item, "shot_number", "分镜序号") or index,
        "image_prompt": _first_value(item, "image_prompt", "图像提示词") or "",
        "video_prompt": _first_value(item, "video_prompt", "视频提示词") or "",
    }


def _normalize_storyboard_image_prompt_item(item: Dict[str, Any], index: int) -> Dict[str, Any]:
    return {
        **item,
        "shot_number": _first_value(item, "shot_number", "分镜序号") or index,
        "image_prompt": _first_value(item, "image_prompt", "图像提示词") or "",
        "video_prompt": _first_value(item, "video_prompt", "视频提示词") or "",
        "duration_suggestion": _first_value(item, "duration_suggestion", "时长建议") or "",
        "negative_prompt": _first_value(item, "negative_prompt", "负面规避词", "负面规避") or "",
    }


def parse_storyboard_items(content: str) -> List[Dict[str, Any]]:
    payload = _parse_json_payload(content)
    items = _extract_storyboard_items(payload)
    if not isinstance(items, list):
        return []
    return [
        normalize_storyboard_item(item, index)
        for index, item in enumerate(items, start=1)
        if isinstance(item, dict)
    ]


def _is_storyboard_image_prompt_generation(generation_type: str) -> bool:
    return generation_type in {"storyboard_image_prompt", "storyboard_image_prompt_generation"}


def normalize_storyboard_item(item: Dict[str, Any], index: int) -> Dict[str, Any]:
    description_prompt = str(
        _first_value(item, "description_prompt", "画面描述", "视频提示词") or ""
    )
    scenes = _as_string_list(_first_value(item, "scenes", "场景", "场景名称"))
    shots = [
        _normalize_agent_group_shot(shot, shot_index)
        for shot_index, shot in enumerate(item.get("shots") or item.get("镜头组") or [], start=1)
        if isinstance(shot, dict)
    ]
    duration_seconds = (
        estimate_agent_shot_group_duration(shots)
        if shots
        else _duration_seconds_from_value(
            _first_value(
                item,
                "estimated_duration_seconds",
                "duration_seconds",
                "duration_suggestion",
                "预估时长",
                "时长建议",
            )
        )
    )
    shot_characters = _ordered_shot_values(shots, "characters")
    shot_props = _ordered_shot_values(shots, "props")
    shot_scenes = _ordered_shot_values(shots, "scene_name")
    first_shot = shots[0] if shots else {}
    return {
        **item,
        "shot_number": _first_value(
            item,
            "group_number",
            "shot_number",
            "storyboard_index",
            "分镜组序号",
            "分镜序号",
            "镜头编号",
        )
        or index,
        "title": _first_value(item, "title", "标题")
        or f"分镜{_first_value(item, 'storyboard_index') or index}",
        "source_content": _first_value(item, "source_content", "original_text", "原文", "原始文本")
        or "",
        "event_goal": _first_value(item, "event_goal", "叙事目标", "事件目标") or "",
        "scene_name": _first_value(item, "scene_name", "scene", "场景名称")
        or (scenes[0] if scenes else "")
        or (shot_scenes[0] if shot_scenes else ""),
        "scene_state": _first_value(item, "scene_state", "场景状态") or "",
        "shot_size": _first_value(item, "shot_size", "景别") or first_shot.get("shot_size") or "",
        "camera_angle": _first_value(item, "camera_angle", "拍摄角度", "机位")
        or first_shot.get("camera_angle")
        or "",
        "camera_movement": _first_value(item, "camera_movement", "运镜")
        or first_shot.get("camera_movement")
        or "",
        "screen_execution": _first_value(item, "screen_execution", "画面执行")
        or _shot_group_screen_execution(shots),
        "characters": _merge_ordered_values(
            _as_string_list(_first_value(item, "characters", "角色", "人物")),
            shot_characters,
        ),
        "props": _merge_ordered_values(
            _as_string_list(_first_value(item, "props", "道具")),
            shot_props,
        ),
        "action": _first_value(item, "action", "动作")
        or _shot_group_screen_execution(shots)
        or description_prompt,
        "character_action": _first_value(item, "character_action", "角色动作") or "",
        "character_expression": _first_value(item, "character_expression", "角色表情") or "",
        "dialogue": _first_value(item, "dialogue", "台词") or _shot_group_dialogue(shots),
        "sound_effect": _first_value(item, "sound_effect", "音效") or "",
        "atmosphere": _first_value(item, "atmosphere", "氛围参考", "画面氛围") or "",
        "image_prompt": "",
        "video_prompt": "",
        "duration_suggestion": f"{duration_seconds}秒" if duration_seconds else "",
        "estimated_duration_seconds": duration_seconds or 0,
        "production_focus": _first_value(item, "production_focus", "制作重点") or "",
        "negative_prompt": "",
        "ending_frame": _first_value(item, "ending_frame", "结尾画面", "收束画面") or "",
        "split_reason": _first_value(item, "split_reason", "拆分理由") or "",
        "shots": shots,
    }


def _normalize_agent_group_shot(item: Dict[str, Any], index: int) -> Dict[str, Any]:
    return {
        "shot_number": index,
        "shot_size": str(_first_value(item, "shot_size", "景别") or "").strip(),
        "camera_shot": str(
            _first_value(item, "camera_shot", "shooting_shot", "拍摄镜头", "镜头拍摄") or ""
        ).strip(),
        "camera_angle": str(_first_value(item, "camera_angle", "拍摄角度", "机位") or "").strip(),
        "camera_movement": str(
            _first_value(item, "camera_movement", "镜头运镜", "运镜") or ""
        ).strip(),
        "visual_content": str(
            _first_value(item, "visual_content", "screen_content", "画面内容", "画面执行") or ""
        ).strip(),
        "scene_name": str(_first_value(item, "scene_name", "场景名称", "场景") or "").strip(),
        "characters": _as_string_list(_first_value(item, "characters", "人物", "角色")),
        "props": _as_string_list(_first_value(item, "props", "道具")),
        "speaker": str(_first_value(item, "speaker", "说话人物", "台词人物") or "").strip(),
        "dialogue": str(_first_value(item, "dialogue", "台词") or "").strip(),
    }


def estimate_agent_shot_group_duration(shots: List[Dict[str, Any]]) -> int:
    chinese_character_count = 0
    english_word_count = 0
    for shot in shots:
        dialogue = str(shot.get("dialogue") or "")
        chinese_character_count += len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", dialogue))
        english_word_count += len(re.findall(r"[A-Za-z]+(?:['’-][A-Za-z]+)*", dialogue))
    dialogue_seconds = math.ceil(chinese_character_count / 4 + english_word_count / 4)
    visual_seconds = max(4, len(shots) * 2)
    return max(visual_seconds, dialogue_seconds)


def build_agent_storyboard_prompt(
    visual_style: str,
    shots: List[Dict[str, Any]],
    duration_seconds: Optional[int] = None,
) -> str:
    lines = [
        f"画面风格：{visual_style.strip() or '沿用项目整体画风'}",
        "视频中不得出现任何字幕、文字叠加，保持纯画面。不要BGM，不要配乐。",
    ]
    for index, shot in enumerate(shots, start=1):
        line = (
            f"镜头{index}：景别：{shot.get('shot_size') or ''}；"
            f"拍摄镜头：{shot.get('camera_shot') or ''}；"
            f"拍摄角度：{shot.get('camera_angle') or ''}；"
            f"镜头运镜：{shot.get('camera_movement') or ''}；"
            f"画面内容：{shot.get('visual_content') or ''}"
        )
        dialogue = str(shot.get("dialogue") or "").strip()
        if dialogue:
            speaker = str(shot.get("speaker") or "人物").strip()
            line += f"；人物说台词：{speaker}：{dialogue}"
        lines.append(line)
    if duration_seconds is not None:
        lines.append(f"分镜组总时长：{duration_seconds}秒")
    return "\n".join(lines)


def prepare_agent_storyboard_groups(
    items: List[Dict[str, Any]],
    visual_style: str,
) -> List[Dict[str, Any]]:
    prepared = []
    for item in items:
        shots = item.get("shots") if isinstance(item.get("shots"), list) else []
        if not shots:
            raise AppException("分镜组必须包含至少一个镜头", code=50231, status_code=502)
        if any(
            not shot.get("shot_size")
            or not shot.get("camera_shot")
            or not shot.get("camera_angle")
            or not shot.get("camera_movement")
            or not shot.get("visual_content")
            for shot in shots
        ):
            raise AppException("分镜组镜头字段不完整", code=50231, status_code=502)
        duration_seconds = estimate_agent_shot_group_duration(shots)
        if not 4 <= duration_seconds <= 15:
            raise AppException(
                "模型未按剪辑规则拆分分镜组，预估时长超出 4-15 秒",
                code=50231,
                status_code=502,
            )
        prompt = build_agent_storyboard_prompt(
            visual_style,
            shots,
            duration_seconds,
        )
        prepared.append(
            {
                **item,
                "duration_suggestion": f"{duration_seconds}秒",
                "estimated_duration_seconds": duration_seconds,
                "video_prompt": prompt,
                "extra": {
                    **(item.get("extra") if isinstance(item.get("extra"), dict) else {}),
                    "agent_shots": shots,
                    "agent_storyboard_prompt": prompt,
                    "agent_storyboard_prompt_template": prompt,
                    "agent_storyboard_prompt_notes": "",
                    "agent_storyboard_revision": 1,
                    "agent_storyboard_status": "ready",
                    "agent_storyboard_origin": "model",
                },
            }
        )
    return prepared


def validate_agent_storyboard_sequence(
    items: List[Dict[str, Any]],
    chapter_content: str,
) -> None:
    normalized_source = _normalize_storyboard_source(chapter_content)
    if not normalized_source:
        raise AppException("当前分集没有可用于分镜分析的正文", code=50231, status_code=502)
    cursor = 0
    covered_length = 0
    for index, item in enumerate(items, start=1):
        if _as_int(item.get("shot_number"), 0) != index:
            raise AppException(
                "模型返回的分镜组序号不连续",
                code=50231,
                status_code=502,
            )
        source_content = _normalize_storyboard_source(item.get("source_content"))
        if not source_content:
            raise AppException(
                "模型返回的分镜组缺少对应原文",
                code=50231,
                status_code=502,
            )
        position = normalized_source.find(source_content, cursor)
        if position < 0:
            raise AppException(
                "模型返回的分镜组原文不属于当前分集或顺序错误",
                code=50231,
                status_code=502,
            )
        cursor = position + len(source_content)
        covered_length += len(source_content)
    if covered_length / len(normalized_source) < 0.8:
        raise AppException(
            "模型返回的分镜组未覆盖当前分集主要剧情",
            code=50231,
            status_code=502,
        )


def _normalize_storyboard_source(value: Any) -> str:
    return re.sub(r"[\W_]+", "", str(value or ""), flags=re.UNICODE).lower()


def _ordered_shot_values(shots: List[Dict[str, Any]], key: str) -> List[str]:
    values: List[str] = []
    for shot in shots:
        raw = shot.get(key)
        items = raw if isinstance(raw, list) else [raw]
        for item in items:
            value = str(item or "").strip()
            if value and value not in values:
                values.append(value)
    return values


def _merge_ordered_values(first: List[str], second: List[str]) -> List[str]:
    return list(dict.fromkeys([*first, *second]))


def _shot_group_screen_execution(shots: List[Dict[str, Any]]) -> str:
    return "\n".join(
        f"镜头{index}：{shot.get('visual_content')}"
        for index, shot in enumerate(shots, start=1)
        if shot.get("visual_content")
    )


def _shot_group_dialogue(shots: List[Dict[str, Any]]) -> str:
    return "\n".join(
        f"{shot.get('speaker') or '人物'}：{shot.get('dialogue')}"
        for shot in shots
        if shot.get("dialogue")
    )


def _duration_seconds_from_value(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", str(value))
    if match is None:
        return None
    seconds = round(float(match.group(0)))
    return seconds if seconds > 0 else None


def _positive_duration_seconds(value: Any) -> Optional[int]:
    duration = _duration_seconds_from_value(value)
    return duration if duration and duration > 0 else None


def _extract_storyboard_items(payload: Any) -> Optional[List[Any]]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return None
    for key in (
        "storyboard_groups",
        "storyboard_units",
        "items",
        "shots",
        "storyboards",
        "分镜组",
        "分镜",
        "分镜列表",
    ):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict) and _looks_like_storyboard_item(value):
            return [value]
    data = payload.get("data") or payload.get("result")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        nested_items = _extract_storyboard_items(data)
        if nested_items:
            return nested_items
    if _looks_like_storyboard_item(payload):
        return [payload]
    for value in payload.values():
        if isinstance(value, list) and any(isinstance(item, dict) for item in value):
            return value
        if isinstance(value, dict):
            nested_items = _extract_storyboard_items(value)
            if nested_items:
                return nested_items
    return None


def _looks_like_storyboard_item(value: Dict[str, Any]) -> bool:
    keys = set(value.keys())
    storyboard_keys = {
        "shot_number",
        "group_number",
        "storyboard_index",
        "title",
        "source_content",
        "original_text",
        "event_goal",
        "scene_name",
        "action",
        "split_reason",
        "scene_state",
        "shot_size",
        "camera_angle",
        "camera_movement",
        "screen_execution",
        "character_action",
        "character_expression",
        "atmosphere",
        "image_prompt",
        "video_prompt",
        "duration_suggestion",
        "production_focus",
        "negative_prompt",
        "ending_frame",
        "shots",
        "分镜组序号",
        "分镜序号",
        "镜头编号",
        "标题",
        "叙事目标",
        "动作",
        "拆分理由",
        "场景状态",
        "景别",
        "拍摄角度",
        "运镜",
        "画面执行",
        "角色动作",
        "角色表情",
        "氛围参考",
        "画面氛围",
        "结尾画面",
        "收束画面",
        "图像提示词",
        "视频提示词",
    }
    return bool(keys & storyboard_keys)


def _parse_json_payload(content: str) -> Any:
    content = _strip_json_code_fence(content or "")
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for match in re.finditer(r"[\{\[]", content):
        try:
            payload, _ = decoder.raw_decode(content[match.start() :])
            return payload
        except json.JSONDecodeError:
            continue
    return {}


def _strip_json_code_fence(content: str) -> str:
    content = content.strip()
    match = re.search(r"```(?:json)?\s*(.*?)```", content, re.S | re.I)
    return match.group(1).strip() if match else content


def _first_value(item: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return None


def _as_int(value: Any, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _as_string_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item not in (None, "")]


def _optional_str(value: Any, max_length: Optional[int] = None) -> Optional[str]:
    if value in (None, ""):
        return None
    text = str(value)
    return text[:max_length] if max_length else text
