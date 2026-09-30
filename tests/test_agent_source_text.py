import hashlib
from types import SimpleNamespace

import pytest

from app.core.exceptions import AppException
from app.services.agent.source_analysis import _source_block_inputs
from app.services.agent.source_text import (
    parse_agent_json_object,
    split_source_text,
    validate_agent_stage_output,
)
from app.services.prompts import render_system_prompt
from app.services.generation.task_records import get_task_record_options
from app.worker import celery_app


def test_split_source_text_preserves_content_offsets_and_hashes() -> None:
    content = "第一场。\n人物甲出现。\n\n第二场。\n人物乙出现。"

    chunks = split_source_text(content, max_characters=12)

    assert "".join(chunk.content for chunk in chunks) == content
    assert [chunk.index for chunk in chunks] == list(range(len(chunks)))
    assert all(chunk.end_offset - chunk.start_offset <= 12 for chunk in chunks)
    assert all(
        chunk.content_hash == hashlib.sha256(chunk.content.encode("utf-8")).hexdigest()
        for chunk in chunks
    )
    assert all(
        left.end_offset == right.start_offset for left, right in zip(chunks, chunks[1:])
    )


def test_split_source_text_prefers_natural_breaks() -> None:
    chunks = split_source_text("甲乙丙丁。戊己庚辛壬癸", max_characters=8)

    assert chunks[0].content == "甲乙丙丁。"


def test_incremental_episode_planning_only_reads_supplement_source() -> None:
    original = "沈砚在旧宅发现玉佩。"
    supplement = "十年后，沈砚带着怀表重返旧宅。"
    content = f"{original}\n\n{supplement}"
    chunks = split_source_text(content, max_characters=12)
    step = SimpleNamespace(
        extra={
            "analysis_scope": "incremental",
            "incremental_source_start": len(original),
            "incremental_source_end": len(content),
            "chunks": [
                {
                    "index": chunk.index,
                    "start_offset": chunk.start_offset,
                    "end_offset": chunk.end_offset,
                    "content_hash": chunk.content_hash,
                }
                for chunk in chunks
            ],
        }
    )

    blocks = _source_block_inputs(step, content)

    assert "".join(block["content"] for block in blocks) == f"\n\n{supplement}"
    assert blocks[0]["source_start"] == len(original)
    assert blocks[-1]["source_end"] == len(content)


def test_parse_agent_json_accepts_fenced_and_embedded_objects() -> None:
    assert parse_agent_json_object('```json\n{"summary":"a"}\n```') == {"summary": "a"}
    assert parse_agent_json_object('结果如下：{"summary":"b"}。') == {"summary": "b"}


def test_parse_agent_json_rejects_non_object() -> None:
    with pytest.raises(AppException) as exc_info:
        parse_agent_json_object("[1, 2]")

    assert exc_info.value.code == 50241


def test_validate_episode_plan_normalizes_episode_numbers() -> None:
    result = validate_agent_stage_output(
        "episode_planning",
        {
            "episodes": [
                {
                    "episode_number": 9,
                    "title": " 起 ",
                    "content": "第一集内容",
                    "opening_hook": "异响",
                    "goal": "调查",
                    "conflict": "受阻",
                    "climax": "追逐",
                    "ending_hook": "门后有人",
                    "estimated_duration_seconds": 90,
                    "estimated_shot_count": 18,
                    "source_start": 0,
                    "source_end": 100,
                },
                {
                    "title": "承",
                    "content": "第二集内容",
                    "opening_hook": "开门",
                    "goal": "逃离",
                    "conflict": "围堵",
                    "climax": "对峙",
                    "hook": "身份揭晓",
                    "estimated_duration_seconds": 90,
                    "estimated_shot_count": 18,
                    "source_block_ids": [1],
                },
            ]
        },
    )

    assert [episode["episode_number"] for episode in result["episodes"]] == [1, 2]
    assert result["episodes"][0]["title"] == "起"
    assert result["episodes"][1]["ending_hook"] == "身份揭晓"


def test_validate_episode_plan_rejects_non_contiguous_source_ranges() -> None:
    payload = {
        "episodes": [
            {
                "title": "第一集",
                "content": "第一集内容",
                "opening_hook": "开场",
                "goal": "目标",
                "conflict": "冲突",
                "climax": "高潮",
                "ending_hook": "结尾",
                "estimated_duration_seconds": 90,
                "estimated_shot_count": 18,
                "source_start": 0,
                "source_end": 40,
            },
            {
                "title": "第二集",
                "content": "第二集内容",
                "opening_hook": "开场",
                "goal": "目标",
                "conflict": "冲突",
                "climax": "高潮",
                "ending_hook": "结尾",
                "estimated_duration_seconds": 90,
                "estimated_shot_count": 18,
                "source_start": 60,
                "source_end": 100,
            },
        ]
    }

    with pytest.raises(AppException) as exc_info:
        validate_agent_stage_output("episode_planning", payload)

    assert exc_info.value.code == 50244


def test_validate_episode_plan_grounds_contiguous_ranges_from_end_quotes() -> None:
    source_text = "甲出门。乙追赶。\n丙关门。"
    first_quote = "乙追赶。"
    result = validate_agent_stage_output(
        "episode_planning",
        {
            "episodes": [
                {
                    "title": "追赶",
                    "content": "甲出门后乙开始追赶",
                    "opening_hook": "甲出门",
                    "goal": "追上甲",
                    "conflict": "距离拉开",
                    "climax": "乙追到门口",
                    "ending_hook": "丙出现",
                    "source_end_quote": first_quote,
                },
                {
                    "title": "关门",
                    "content": "丙突然关门",
                    "opening_hook": "丙出现",
                    "goal": "阻止追赶",
                    "conflict": "乙被挡住",
                    "climax": "丙关门",
                    "ending_hook": "门后有响声",
                    "source_end_quote": "丙关门。",
                },
            ]
        },
        source_text=source_text,
    )

    boundary = source_text.index(first_quote) + len(first_quote)
    assert [
        (episode["source_start"], episode["source_end"])
        for episode in result["episodes"]
    ] == [(0, boundary), (boundary, len(source_text))]
    assert all("estimated_duration_seconds" not in episode for episode in result["episodes"])
    assert all("estimated_shot_count" not in episode for episode in result["episodes"])


def test_validate_episode_plan_rejects_unverifiable_end_quote() -> None:
    with pytest.raises(AppException) as exc_info:
        validate_agent_stage_output(
            "episode_planning",
            {
                "episodes": [
                    {
                        "title": "第一集",
                        "content": "模型给出的分集内容",
                        "opening_hook": "开场",
                        "goal": "目标",
                        "conflict": "冲突",
                        "climax": "高潮",
                        "ending_hook": "结尾",
                        "source_start": 0,
                        "source_end": 999,
                    }
                ]
            },
            source_text="实际原文。",
        )

    assert exc_info.value.code == 50244


def test_validate_asset_analysis_requires_named_assets_and_keeps_all_groups() -> None:
    result = validate_agent_stage_output(
        "asset_analysis",
        {
            "characters": [
                {
                    "name": "沈砚",
                    "aliases": ["阿砚"],
                    "source_facts": {"story_role": "调查者"},
                    "design_spec": {"identity_anchors": ["黑色短发"]},
                    "source_evidence": [{"source_start": 0, "source_end": 10}],
                }
            ],
            "character_variants": [],
            "scenes": [
                {
                    "name": "旧宅",
                    "source_facts": {"story_role": "主要调查地点"},
                    "design_spec": {"identity_anchors": ["木质祠堂"]},
                    "source_evidence": [{"source_start": 10, "source_end": 20}],
                }
            ],
            "scene_variants": [],
            "props": [
                {
                    "name": "玉佩",
                    "source_facts": {"story_role": "关键线索"},
                    "design_spec": {"identity_anchors": ["双鱼纹"]},
                    "source_evidence": [{"source_start": 20, "source_end": 30}],
                }
            ],
            "prop_variants": [],
        },
    )

    assert result["characters"][0]["name"] == "沈砚"
    assert result["characters"][0]["design_spec"]["identity_anchors"] == ["黑色短发"]
    assert result["scenes"][0]["name"] == "旧宅"
    assert result["props"][0]["name"] == "玉佩"

    with pytest.raises(AppException) as exc_info:
        validate_agent_stage_output(
            "asset_analysis",
            {
                "characters": [{"description": "缺少名称"}],
                "character_variants": [],
                "scenes": [],
                "scene_variants": [],
                "props": [],
                "prop_variants": [],
            },
        )

    assert exc_info.value.code == 50245


def test_validate_asset_analysis_rejects_variant_evidence_outside_source() -> None:
    source_text = "沈砚在旧宅拿起玉佩。"
    payload = {
        "characters": [],
        "character_variants": [],
        "scenes": [],
        "scene_variants": [],
        "props": [
            {
                "name": "玉佩",
                "source_evidence": [{"source_start": 7, "source_end": 9}],
            }
        ],
        "prop_variants": [
            {
                "base_name": "玉佩",
                "name": "玉佩使用状态",
                "variant_type": "state",
                "description": "被沈砚拿起",
                "trigger_reason": "沈砚拿起玉佩",
                "source_evidence": [{"source_start": 50, "source_end": 60}],
            }
        ],
    }

    with pytest.raises(AppException) as exc_info:
        validate_agent_stage_output(
            "asset_analysis",
            payload,
            source_text=source_text,
        )

    assert exc_info.value.code == 50245


def test_validate_asset_analysis_requires_exact_source_quote() -> None:
    source_text = "沈砚在旧宅拿起玉佩。"
    payload = {
        "characters": [],
        "character_variants": [],
        "scenes": [],
        "scene_variants": [],
        "props": [
            {
                "name": "玉佩",
                "source_facts": {"story_role": "关键线索"},
                "design_spec": {"identity_anchors": ["双鱼纹"]},
                "source_evidence": [
                    {
                        "source_start": 7,
                        "source_end": 9,
                        "source_quote": "玉佩",
                    }
                ],
            }
        ],
        "prop_variants": [
            {
                "base_name": "玉佩",
                "name": "玉佩使用状态",
                "variant_type": "state",
                "description": "被沈砚拿起",
                "trigger_reason": "沈砚拿起玉佩",
                "source_evidence": [{"source_start": 5, "source_end": 9}],
            }
        ],
    }

    with pytest.raises(AppException) as exc_info:
        validate_agent_stage_output(
            "asset_analysis",
            payload,
            source_text=source_text,
        )

    assert exc_info.value.code == 50245


def test_validate_asset_analysis_grounds_variant_range_from_exact_quote() -> None:
    source_text = "沈砚在旧宅拿起玉佩。"
    quote = "拿起玉佩"
    payload = {
        "characters": [],
        "character_variants": [],
        "scenes": [],
        "scene_variants": [],
        "props": [
            {
                "name": "玉佩",
                "source_facts": {"story_role": "关键线索"},
                "design_spec": {"identity_anchors": ["双鱼纹"]},
                "source_evidence": [
                    {
                        "source_start": 50,
                        "source_end": 60,
                        "source_quote": "玉佩",
                    }
                ],
            }
        ],
        "prop_variants": [
            {
                "base_name": "玉佩",
                "name": "玉佩使用状态",
                "variant_type": "state",
                "description": "被沈砚拿起",
                "trigger_reason": "沈砚拿起玉佩",
                "visual_delta": {"add": ["表面发出微光"], "replace": [], "remove": []},
                "preserve_anchors": ["双鱼纹和青白色材质不变"],
                "source_evidence": [
                    {
                        "source_start": 50,
                        "source_end": 60,
                        "source_quote": quote,
                    }
                ],
            }
        ],
    }

    result = validate_agent_stage_output(
        "asset_analysis",
        payload,
        source_text=source_text,
    )

    evidence = result["prop_variants"][0]["source_evidence"][0]
    assert evidence["source_start"] == source_text.index(quote)
    assert evidence["source_end"] == source_text.index(quote) + len(quote)


def test_agent_prompts_render_without_unresolved_variables() -> None:
    chunk_prompt = render_system_prompt(
        "agent_source_chunk_analysis.md",
        chunk_number="1",
        chunk_count="1",
        start_offset="0",
        end_offset="2",
        input_text="正文",
    )
    asset_prompt = render_system_prompt(
        "agent_asset_analysis.md",
        episodes_json="[]",
        existing_assets_json="{}",
    )
    episode_prompt = render_system_prompt(
        "agent_episode_planning.md",
        source_blocks_json="[]",
    )

    assert "正文" in chunk_prompt
    assert "分集剧本" in asset_prompt
    assert "原始剧本块" in episode_prompt
    assert "目标集数" not in episode_prompt
    assert "单集目标时长" not in episode_prompt
    assert "默认镜头时长" not in episode_prompt
    assert all("{{" not in prompt for prompt in (chunk_prompt, asset_prompt, episode_prompt))


def test_agent_task_types_and_workers_are_registered() -> None:
    generation_types = {
        item["value"] for item in get_task_record_options()["generation_types"]
    }

    assert {
        "agent_source_chunk_analysis",
        "agent_asset_analysis",
        "agent_source_global_merge",
        "agent_episode_planning",
    }.issubset(generation_types)
    assert "tasks.agent_source_analysis.run_source_analysis" in celery_app.tasks
    assert "tasks.agent_source_analysis.run_agent_text_task" in celery_app.tasks
