from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.core.exceptions import AppException
from app.main import app
from app.models.agent_story_bible import AgentAssetCandidate, AgentAssetVariant
from app.schemas.agent_script_package import (
    AgentScriptPackageConfirmRequest,
    AgentScriptPackageOut,
    AgentScriptSupplementRequest,
)
from app.schemas.agent_story_bible import (
    AgentAssetCandidateOut,
    AgentAssetVariantUpdateRequest,
)
from app.services.agent.source_text import validate_agent_stage_output
from app.services.agent.story_bibles import (
    _merge_incremental_bible_content,
    build_asset_candidate_payloads,
    build_asset_variant_payloads,
    sync_asset_candidate_episode_links,
    sync_asset_variant_episode_links,
)
from app.services.agent.workbench import resolve_workbench_actions


def _candidate(asset_type: str, name: str, aliases=None) -> AgentAssetCandidate:
    return AgentAssetCandidate(
        id=uuid4(),
        project_id=uuid4(),
        production_id=uuid4(),
        bible_version_id=uuid4(),
        user_id=uuid4(),
        asset_type=asset_type,
        candidate_key=(name.encode("utf-8").hex() + "0" * 64)[:64],
        canonical_name=name,
        aliases=aliases or [],
        source_chapter_ids=[],
        confidence=1,
        merge_reason="",
        review_status="ready",
        content={},
        lock_version=0,
    )


def test_script_package_routes_are_registered() -> None:
    routes = {
        (path, method.upper())
        for path, methods in app.openapi()["paths"].items()
        for method in methods
    }
    assert {
        ("/api/v1/agent-productions/{production_id}/script-package", "GET"),
        (
            "/api/v1/agent-productions/{production_id}/script-supplements/from-text",
            "POST",
        ),
        (
            "/api/v1/agent-productions/{production_id}/script-supplements/from-file",
            "POST",
        ),
        (
            "/api/v1/agent-productions/{production_id}/asset-variants/{variant_id}",
            "PATCH",
        ),
        ("/api/v1/agent-productions/{production_id}/script-package/confirm", "POST"),
    } <= routes


def test_script_supplement_requires_non_empty_content() -> None:
    payload = AgentScriptSupplementRequest(content="  新增剧情  ")
    assert payload.content == "新增剧情"

    with pytest.raises(ValidationError):
        AgentScriptSupplementRequest(content="   ")


def test_incremental_assets_merge_aliases_and_keep_new_assets() -> None:
    content = _merge_incremental_bible_content(
        {
            "characters": [
                {"name": "沈砚", "aliases": ["阿砚"], "appearance": "黑发"}
            ],
            "character_variants": [],
            "scenes": [{"name": "旧宅"}],
            "scene_variants": [],
            "props": [],
            "prop_variants": [],
        },
        {
            "characters": [
                {"name": "沈先生", "aliases": ["沈砚"], "appearance": "黑发灰眼"},
                {"name": "林晚", "aliases": []},
            ],
            "character_variants": [
                {
                    "base_name": "沈砚",
                    "name": "沈砚中年造型",
                    "variant_type": "age",
                    "description": "鬓角斑白",
                    "trigger_reason": "十年后",
                    "source_start": 100,
                    "source_end": 110,
                }
            ],
            "scenes": [],
            "scene_variants": [],
            "props": [{"name": "怀表"}],
            "prop_variants": [],
        },
    )
    candidates = build_asset_candidate_payloads(content, {"episodes": []}, [])

    characters = [item for item in candidates if item["asset_type"] == "character"]
    assert len(characters) == 2
    shenyan = next(item for item in characters if item["canonical_name"] == "沈砚")
    assert {"阿砚", "沈先生"} <= set(shenyan["aliases"])
    assert any(item["canonical_name"] == "怀表" for item in candidates)
    assert content["character_variants"][0]["name"] == "沈砚中年造型"


def test_candidate_builder_keeps_story_design_and_builds_generation_fields() -> None:
    payloads = build_asset_candidate_payloads(
        {
            "characters": [
                {
                    "name": "沈砚",
                    "aliases": [],
                    "source_facts": {
                        "story_role": "追查玉佩失踪事件的调查者",
                        "gender": "男",
                    },
                    "design_spec": {
                        "apparent_age": "二十七八岁",
                        "body_type": "高挑偏瘦",
                        "hair_style": "利落黑色短发",
                        "default_costume": "黑色长风衣",
                        "temperament": "沉稳警觉",
                        "identity_anchors": ["黑色短发", "窄长脸"],
                        "design_rationale": "冷色造型符合悬疑调查剧情",
                    },
                    "source_evidence": [{"source_start": 0, "source_end": 10}],
                }
            ]
        },
        {"episodes": [{"source_start": 0, "source_end": 20}]},
        [],
    )

    content = payloads[0]["content"]
    assert content["source_facts"]["story_role"] == "追查玉佩失踪事件的调查者"
    assert content["design_spec"]["identity_anchors"] == ["黑色短发", "窄长脸"]
    assert content["age"] == "二十七八岁"
    assert "高挑偏瘦" in content["appearance"]
    assert content["costume"] == "黑色长风衣"
    assert "身份固定特征" in content["prompt"]


def test_script_package_openapi_describes_three_asset_groups() -> None:
    properties = AgentScriptPackageOut.model_json_schema()["properties"]

    expected_descriptions = {
        "characters": "人物基础资产",
        "character_variants": "人物变装或状态变体",
        "scenes": "场景基础资产",
        "scene_variants": "场景时间、天气、季节、损坏等变体",
        "props": "道具基础资产",
        "prop_variants": "道具形态、损坏、开合、归属等变体",
    }
    for field_name, description in expected_descriptions.items():
        assert description in properties[field_name]["description"]


def test_variant_builder_links_base_asset_merges_evidence_and_episodes() -> None:
    character = _candidate("character", "沈砚", ["阿砚"])
    scene = _candidate("scene", "旧宅")
    prop = _candidate("prop", "玉佩")
    bible = {
        "character_variants": [
            {
                "base_name": "阿砚",
                "name": "受伤造型",
                "variant_type": "injury",
                "description": "额角流血",
                "trigger_reason": "遭到袭击",
                "source_start": 20,
                "source_end": 30,
                "confidence": 0.95,
            },
            {
                "base_name": "沈砚",
                "name": "受伤造型",
                "variant_type": "injury",
                "description": "额角流血，衣领沾血",
                "trigger_reason": "遭到袭击后继续追查",
                "source_start": 120,
                "source_end": 130,
                "confidence": 0.9,
            },
            {
                "base_name": "不存在的人",
                "name": "礼服",
                "variant_type": "costume",
            },
        ],
        "scene_variants": [
            {
                "base_name": "旧宅",
                "name": "雨夜旧宅",
                "variant_type": "weather",
                "source_start": 40,
                "source_end": 60,
            }
        ],
        "prop_variants": [
            {
                "base_name": "玉佩",
                "name": "碎裂玉佩",
                "variant_type": "damage",
                "source_start": 150,
                "source_end": 155,
            }
        ],
    }
    episode_plan = {
        "episodes": [
            {"episode_number": 1, "source_start": 0, "source_end": 100},
            {"episode_number": 2, "source_start": 100, "source_end": 200},
        ]
    }

    payloads, warnings = build_asset_variant_payloads(
        bible,
        episode_plan,
        [character, scene, prop],
    )

    assert len(payloads) == 3
    injury = next(item for item in payloads if item["asset_type"] == "character")
    assert injury["base_candidate_id"] == character.id
    assert injury["episode_numbers"] == [1, 2]
    assert injury["description"] == "额角流血，衣领沾血"
    assert injury["trigger_reason"] == "遭到袭击后继续追查"
    assert injury["source_evidence"] == [
        {"source_start": 20, "source_end": 30},
        {"source_start": 120, "source_end": 130},
    ]
    assert warnings[0]["code"] == "invalid_asset_variant"


def test_base_asset_response_includes_derived_episode_numbers() -> None:
    character = _candidate("character", "沈砚")
    character.content = {
        "source_evidence": [{"source_start": 120, "source_end": 130}]
    }
    plan = {
        "episodes": [
            {"episode_number": 1, "source_start": 0, "source_end": 100},
            {"episode_number": 2, "source_start": 100, "source_end": 200},
        ]
    }

    sync_asset_candidate_episode_links([character], plan)

    assert character.content["episode_numbers"] == [2]
    assert plan["episodes"][0]["characters"] == []
    assert plan["episodes"][1]["characters"] == ["沈砚"]
    assert character.episode_numbers == [2]
    assert "episode_numbers" in AgentAssetCandidateOut.model_json_schema()["properties"]


def test_variant_episode_links_are_rebuilt_after_episode_split() -> None:
    first_candidate = _candidate("character", "沈砚")
    second_candidate = _candidate("prop", "玉佩")
    variants = [
        AgentAssetVariant(
            base_candidate_id=first_candidate.id,
            project_id=first_candidate.project_id,
            production_id=first_candidate.production_id,
            bible_version_id=first_candidate.bible_version_id,
            user_id=first_candidate.user_id,
            asset_type="character",
            variant_key="a" * 64,
            canonical_name="沈砚受伤造型",
            variant_type="injury",
            description="额角流血",
            trigger_reason="遭到袭击",
            episode_numbers=[1],
            source_evidence=[{"source_start": 10, "source_end": 20}],
            confidence=1,
            review_status="ready",
            content={},
        ),
        AgentAssetVariant(
            base_candidate_id=second_candidate.id,
            project_id=second_candidate.project_id,
            production_id=second_candidate.production_id,
            bible_version_id=second_candidate.bible_version_id,
            user_id=second_candidate.user_id,
            asset_type="prop",
            variant_key="b" * 64,
            canonical_name="碎裂玉佩",
            variant_type="damage",
            description="裂成两半",
            trigger_reason="对峙中被击碎",
            episode_numbers=[1],
            source_evidence=[{"source_start": 120, "source_end": 130}],
            confidence=1,
            review_status="ready",
            content={},
        ),
    ]
    plan = {
        "episodes": [
            {"episode_number": 1, "source_start": 0, "source_end": 100},
            {"episode_number": 2, "source_start": 100, "source_end": 200},
        ]
    }

    sync_asset_variant_episode_links(variants, plan)

    assert variants[0].episode_numbers == [1]
    assert variants[1].episode_numbers == [2]
    assert plan["episodes"][0]["character_variants"] == ["沈砚受伤造型"]
    assert plan["episodes"][0]["prop_variants"] == []
    assert plan["episodes"][1]["character_variants"] == []
    assert plan["episodes"][1]["prop_variants"] == ["碎裂玉佩"]


def test_variant_builder_maps_source_evidence_list_to_episodes() -> None:
    character = _candidate("character", "沈砚")
    payloads, warnings = build_asset_variant_payloads(
        {
            "character_variants": [
                {
                    "base_name": "沈砚",
                    "name": "沈砚受伤造型",
                    "variant_type": "injury",
                    "description": "额角流血",
                    "trigger_reason": "遭到袭击",
                    "source_evidence": [
                        {"source_start": 120, "source_end": 130}
                    ],
                }
            ]
        },
        {
            "episodes": [
                {"episode_number": 1, "source_start": 0, "source_end": 100},
                {"episode_number": 2, "source_start": 100, "source_end": 200},
            ]
        },
        [character],
    )

    assert warnings == []
    assert payloads[0]["episode_numbers"] == [2]
    assert payloads[0]["review_status"] == "ready"


def test_text_stage_contract_keeps_all_three_variant_lists() -> None:
    result = validate_agent_stage_output(
        "global_merge",
        {
            "story_summary": "完整剧情",
            "character_variants": [
                {
                    "base_name": "沈砚",
                    "name": "受伤造型",
                    "variant_type": "injury",
                    "description": "额角流血",
                    "trigger_reason": "遭到袭击",
                    "source_start": 10,
                    "source_end": 20,
                }
            ],
            "scene_variants": [
                {
                    "base_name": "旧宅",
                    "name": "雨夜旧宅",
                    "variant_type": "weather",
                    "description": "暴雨中的旧宅",
                    "trigger_reason": "故事发生在雨夜",
                    "source_start": 20,
                    "source_end": 30,
                }
            ],
            "prop_variants": [
                {
                    "base_name": "玉佩",
                    "name": "碎裂玉佩",
                    "variant_type": "damage",
                    "description": "裂成两半",
                    "trigger_reason": "对峙中被击碎",
                    "source_start": 30,
                    "source_end": 40,
                }
            ],
        },
    )

    assert len(result["character_variants"]) == 1
    assert len(result["scene_variants"]) == 1
    assert len(result["prop_variants"]) == 1


def test_text_stage_contract_rejects_incomplete_or_unknown_variant() -> None:
    with pytest.raises(AppException):
        validate_agent_stage_output(
            "global_merge",
            {
                "story_summary": "完整剧情",
                "character_variants": [
                    {
                        "base_name": "沈砚",
                        "name": "未知造型",
                        "variant_type": "unknown",
                        "description": "未知",
                        "trigger_reason": "未知",
                        "source_start": 10,
                        "source_end": 20,
                    }
                ],
            },
        )


def test_variant_contract_has_independent_reference_image_field() -> None:
    assert "reference_image" in AgentAssetVariant.__table__.columns
    assert "extra" in AgentAssetVariant.__table__.columns
    assert {
        "ck_agent_asset_variants_variant_type",
        "fk_agent_asset_variants_consistent_base",
    } <= {constraint.name for constraint in AgentAssetVariant.__table__.constraints}
    payload = AgentAssetVariantUpdateRequest(
        expected_lock_version=0,
        episode_numbers=[2, 1, 2],
    )
    assert payload.episode_numbers == [1, 2]
    with pytest.raises(ValidationError):
        AgentAssetVariantUpdateRequest(
            expected_lock_version=0,
            episode_numbers=[0],
        )
    with pytest.raises(ValidationError):
        AgentAssetVariantUpdateRequest(
            expected_lock_version=0,
            source_evidence=[{"source_start": 20, "source_end": 10}],
        )


def test_script_confirm_contract_and_workbench_action() -> None:
    payload = AgentScriptPackageConfirmRequest(
        expected_script_version=2,
        expected_bible_version=1,
        idempotency_key="  script-confirm-v2  ",
    )
    assert payload.idempotency_key == "script-confirm-v2"
    assert resolve_workbench_actions("waiting_approval", "script_review") == (
        "review_script",
        ["review_script", "cancel"],
    )


def test_prompts_require_base_link_and_never_request_reference_images() -> None:
    prompt_dir = Path("app/prompts/system")
    prompts = [
        (prompt_dir / "agent_source_chunk_analysis.md").read_text(),
        (prompt_dir / "agent_asset_analysis.md").read_text(),
        (prompt_dir / "agent_source_global_merge.md").read_text(),
        (prompt_dir / "agent_episode_planning.md").read_text(),
    ]
    assert "base_name" in prompts[0]
    assert "character_variants" in prompts[0]
    assert "scene_variants" in prompts[1]
    assert "prop_variants" in prompts[1]
    assert all("reference_image" not in prompt for prompt in prompts)
