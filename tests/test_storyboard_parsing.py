import json

import pytest

from app.services.projects.storyboard_parsing import (
    parse_storyboard_items,
    parse_storyboard_stage_items,
)


@pytest.mark.parametrize("wrapper", ["list", "nested", "fence", "surrounding_text"])
def test_analysis_parser_preserves_chinese_fields_and_ignores_non_items(wrapper):
    item = {
        "分镜序号": 2,
        "标题": "进门",
        "原文": "他推开门。",
        "人物": ["小林", "小林"],
        "场景名称": "旧宅",
        "时长建议": "4.5秒",
        "视频提示词": "旧提示词",
        "自定义字段": {"保留": True},
    }
    payload = [None, item, "ignore"]
    if wrapper == "nested":
        payload = {"result": {"storyboard_units": payload}}
    content = json.dumps(payload, ensure_ascii=False)
    if wrapper == "fence":
        content = f"```JSON\n{content}\n```"
    elif wrapper == "surrounding_text":
        content = f"以下是结果：\n{content}\n完成。"
    items = parse_storyboard_items(content)
    assert len(items) == 1
    result = items[0]
    assert result["shot_number"] == 2
    assert result["title"] == "进门"
    assert result["source_content"] == "他推开门。"
    assert result["scene_name"] == "旧宅"
    assert result["characters"] == ["小林"]
    assert result["duration_suggestion"] == "4秒"
    assert result["video_prompt"] == ""
    assert result["自定义字段"] == {"保留": True}


@pytest.mark.parametrize(
    "generation_type,key",
    [
        ("storyboard_refinement", "storyboard_execution_item"),
        ("storyboard_prompt_generation", "storyboard_prompt_item"),
        ("storyboard_image_prompt", "image_prompt_item"),
        ("storyboard_image_prompt_generation", "image_prompt_item"),
    ],
)
def test_stage_parser_accepts_single_item_and_legacy_generation_alias(generation_type, key):
    content = json.dumps(
        {key: {"分镜序号": 3, "标题": "转身", "视频提示词": "拉远", "场景状态": "夜晚"}}
    )
    (item,) = parse_storyboard_stage_items(content, generation_type)
    assert item["shot_number"] == 3
    if generation_type == "storyboard_refinement":
        assert item["title"] == "转身"
        assert item["scene_state"] == "夜晚"
    else:
        assert item["video_prompt"] == "拉远"


@pytest.mark.parametrize("content", ["", "不是 JSON", "42", '{"items": []}', '[null, "ignored"]'])
def test_parser_returns_no_items_for_unusable_model_output(content):
    assert parse_storyboard_items(content) == []
    assert parse_storyboard_stage_items(content, "storyboard_refinement") == []
