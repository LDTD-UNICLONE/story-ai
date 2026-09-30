import re
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException
from app.models.agent_core_asset import AgentCoreAssetLock
from app.models.agent_production import AgentProduction
from app.models.agent_story_bible import AgentAssetCandidate, AgentAssetVariant
from app.models.project_asset import ProjectCharacter, ProjectProp, ProjectScene
from app.models.project_chapter import ProjectChapter
from app.models.project_storyboard import ProjectStoryboard
from app.services.agent.storyboard_prompts import (
    render_asset_tokens,
    tokenize_asset_text,
    tokenize_storyboard_shots,
)


ASSET_MODELS = {
    "character": ProjectCharacter,
    "scene": ProjectScene,
    "prop": ProjectProp,
}
AGENT_ASSET_BINDING_VERSION = 3


async def bind_storyboards_to_core_lock(
    db: AsyncSession,
    production: AgentProduction,
    core_lock: AgentCoreAssetLock,
    storyboards: List[ProjectStoryboard],
) -> None:
    indexes: Dict[str, Dict[str, List[UUID]]] = {
        "character": {},
        "scene": {},
        "prop": {},
    }
    variant_indexes: Dict[str, Dict[str, List[tuple[UUID, UUID, set[int], str]]]] = {
        "character": {},
        "scene": {},
        "prop": {},
    }
    variant_details: Dict[UUID, AgentAssetVariant] = {}
    asset_episode_numbers: Dict[tuple[str, UUID], set[int]] = {}
    asset_names: Dict[str, Dict[UUID, str]] = {key: {} for key in ASSET_MODELS}
    snapshots: Dict[str, List[Dict[str, Any]]] = {key: [] for key in ASSET_MODELS}
    for item in core_lock.assets or []:
        asset_type = str(item.get("asset_type") or "")
        if asset_type in snapshots:
            snapshots[asset_type].append(item)
    for asset_type, model in ASSET_MODELS.items():
        ids = [_optional_uuid(item.get("asset_id")) for item in snapshots[asset_type]]
        valid_ids = [value for value in ids if value is not None]
        if not valid_ids:
            continue
        result = await db.execute(
            select(model).where(
                model.id.in_(valid_ids),
                model.project_id == production.project_id,
                model.user_id == production.user_id,
                model.is_enabled.is_(True),
            )
        )
        for asset in result.scalars().all():
            asset_names[asset_type][asset.id] = asset.name
            names = [asset.name]
            if isinstance(asset, ProjectCharacter):
                names.extend(asset.aliases or [])
            for name in names:
                normalized = _normalize_name(name)
                if normalized:
                    indexes[asset_type].setdefault(normalized, []).append(asset.id)

    selected_candidates: Dict[UUID, tuple[str, UUID]] = {}
    for asset_type, items in snapshots.items():
        for item in items:
            candidate_id = _optional_uuid(item.get("candidate_id"))
            asset_id = _optional_uuid(item.get("asset_id"))
            if candidate_id is not None and asset_id is not None:
                selected_candidates[candidate_id] = (asset_type, asset_id)
    if selected_candidates:
        candidate_result = await db.execute(
            select(AgentAssetCandidate).where(
                AgentAssetCandidate.id.in_(selected_candidates),
                AgentAssetCandidate.production_id == production.id,
                AgentAssetCandidate.bible_version_id == core_lock.bible_version_id,
                AgentAssetCandidate.review_status == "materialized",
            )
        )
        candidates = list(candidate_result.scalars().all())
        selected_candidates = {
            candidate.id: selected_candidates[candidate.id]
            for candidate in candidates
            if candidate.id in selected_candidates
            and candidate.materialized_asset_id == selected_candidates[candidate.id][1]
        }
        for candidate in candidates:
            selected = selected_candidates.get(candidate.id)
            if selected is None:
                continue
            asset_type, asset_id = selected
            asset_episode_numbers[(asset_type, asset_id)] = set(candidate.episode_numbers)
            for name in [candidate.canonical_name, *(candidate.aliases or [])]:
                normalized = _normalize_name(name)
                if normalized:
                    indexes[asset_type].setdefault(normalized, []).append(asset_id)

        if selected_candidates:
            variant_result = await db.execute(
                select(AgentAssetVariant).where(
                    AgentAssetVariant.base_candidate_id.in_(selected_candidates),
                    AgentAssetVariant.production_id == production.id,
                    AgentAssetVariant.bible_version_id == core_lock.bible_version_id,
                    AgentAssetVariant.review_status == "ready",
                )
            )
            for variant in variant_result.scalars().all():
                variant_details[variant.id] = variant
                selected = selected_candidates.get(variant.base_candidate_id)
                normalized = _normalize_name(variant.canonical_name)
                if selected is None or not normalized:
                    continue
                asset_type, asset_id = selected
                episode_numbers = {
                    int(value)
                    for value in variant.episode_numbers or []
                    if str(value).isdigit()
                }
                variant_indexes[asset_type].setdefault(normalized, []).append(
                    (asset_id, variant.id, episode_numbers, variant.canonical_name)
                )

    chapter_episode_numbers: Dict[UUID, int] = {}
    if asset_episode_numbers or any(
        variant_indexes[asset_type] for asset_type in variant_indexes
    ):
        chapter_ids = {storyboard.chapter_id for storyboard in storyboards}
        if chapter_ids:
            chapter_result = await db.execute(
                select(ProjectChapter).where(ProjectChapter.id.in_(chapter_ids))
            )
            chapter_episode_numbers = {
                chapter.id: int((chapter.extra or {}).get("episode_number") or 0)
                for chapter in chapter_result.scalars().all()
            }

    for storyboard in storyboards:
        if _preserve_manual_bindings(storyboard, core_lock):
            continue
        requested = {
            "character": [str(value) for value in storyboard.characters or []],
            "scene": [storyboard.scene_name] if storyboard.scene_name else [],
            "prop": [str(value) for value in storyboard.props or []],
        }
        bound: Dict[str, List[str]] = {key: [] for key in ASSET_MODELS}
        bindings: List[Dict[str, str]] = []
        binding_labels: Dict[str, str] = {}
        name_to_key: Dict[str, str] = {}
        binding_keys_by_asset: Dict[tuple[str, UUID], str] = {}
        variant_ids: Dict[str, Dict[str, str]] = {key: {} for key in ASSET_MODELS}
        variant_context: List[Dict[str, str]] = []
        unbound: Dict[str, List[str]] = {key: [] for key in ASSET_MODELS}
        episode_number = chapter_episode_numbers.get(storyboard.chapter_id, 0)
        for asset_type, names in requested.items():
            for name in names:
                normalized = _normalize_name(name)
                matches = [
                    (asset_id, None, asset_names[asset_type].get(asset_id, name))
                    for asset_id in indexes[asset_type].get(normalized, [])
                    if not asset_episode_numbers.get((asset_type, asset_id))
                    or episode_number
                    in asset_episode_numbers[(asset_type, asset_id)]
                ]
                matches.extend(
                    (asset_id, variant_id, variant_name)
                    for asset_id, variant_id, episode_numbers, variant_name in variant_indexes[
                        asset_type
                    ].get(normalized, [])
                    if not episode_numbers or episode_number in episode_numbers
                )
                matches = list(dict.fromkeys(matches))
                if len(matches) == 1:
                    asset_id, variant_id, label = matches[0]
                    value = str(asset_id)
                    if value not in bound[asset_type]:
                        bound[asset_type].append(value)
                        binding_key = f"{asset_type}_{len(bound[asset_type])}"
                        binding_keys_by_asset[(asset_type, asset_id)] = binding_key
                        binding = {
                            "binding_key": binding_key,
                            "asset_type": asset_type,
                            "asset_id": value,
                        }
                        if variant_id is not None:
                            variant = variant_details[variant_id]
                            binding["variant_id"] = str(variant_id)
                            variant_ids[asset_type][value] = str(variant_id)
                            variant_context.append(
                                {
                                    "binding_key": binding_key,
                                    "asset_type": asset_type,
                                    "asset_id": value,
                                    "variant_id": str(variant_id),
                                    "name": label,
                                    "variant_type": variant.variant_type,
                                    "description": variant.description,
                                    "trigger_reason": variant.trigger_reason,
                                }
                            )
                        bindings.append(binding)
                        binding_labels[binding_key] = label
                    binding_key = binding_keys_by_asset[(asset_type, asset_id)]
                    name_to_key[name] = binding_key
                    base_name = asset_names[asset_type].get(asset_id)
                    if base_name:
                        name_to_key[base_name] = binding_key
                    if label:
                        name_to_key[label] = binding_key
                elif name and name not in unbound[asset_type]:
                    unbound[asset_type].append(name)
        extra = dict(storyboard.extra or {})
        current_prompt = str(
            extra.get("agent_storyboard_prompt_template")
            or extra.get("agent_storyboard_prompt")
            or storyboard.video_prompt
            or ""
        )
        prompt_template = tokenize_asset_text(current_prompt, name_to_key)
        tokenized_shots = tokenize_storyboard_shots(
            list(extra.get("agent_shots") or []),
            name_to_key,
        )
        rendered_prompt = render_asset_tokens(prompt_template, binding_labels)
        storyboard.video_prompt = rendered_prompt
        storyboard.extra = {
            **extra,
            "agent_asset_ids": bound,
            "agent_asset_bindings": bindings,
            "agent_asset_variant_ids": variant_ids,
            "agent_asset_variant_context": variant_context,
            "agent_asset_binding_labels": binding_labels,
            "agent_unbound_asset_names": unbound,
            "agent_core_asset_lock_id": str(core_lock.id),
            "agent_core_asset_lock_version": core_lock.version,
            "agent_asset_binding_version": AGENT_ASSET_BINDING_VERSION,
            "agent_asset_binding_source": "analysis",
            "agent_shots": tokenized_shots,
            "agent_storyboard_prompt_template": prompt_template,
            "agent_storyboard_prompt": rendered_prompt,
            "agent_storyboard_revision": int(extra.get("agent_storyboard_revision") or 1),
            "agent_storyboard_status": str(
                extra.get("agent_storyboard_status") or "ready"
            ),
            "agent_storyboard_origin": str(
                extra.get("agent_storyboard_origin") or "model"
            ),
        }


async def bind_agent_storyboard_analysis_result(
    db: AsyncSession,
    production_id: UUID,
    storyboards: List[ProjectStoryboard],
) -> None:
    production_result = await db.execute(
        select(AgentProduction).where(AgentProduction.id == production_id)
    )
    production = production_result.scalar_one_or_none()
    if production is None:
        raise AppException("整剧任务不存在", code=40430, status_code=404)
    lock_result = await db.execute(
        select(AgentCoreAssetLock).where(
            AgentCoreAssetLock.production_id == production.id,
            AgentCoreAssetLock.project_id == production.project_id,
            AgentCoreAssetLock.status == "active",
        )
    )
    core_lock = lock_result.scalar_one_or_none()
    if core_lock is None:
        raise AppException("核心资产尚未锁定", code=40950, status_code=409)
    await bind_storyboards_to_core_lock(db, production, core_lock, storyboards)


def _preserve_manual_bindings(
    storyboard: ProjectStoryboard,
    core_lock: AgentCoreAssetLock,
) -> bool:
    extra = storyboard.extra or {}
    return (
        extra.get("agent_asset_binding_source") == "user"
        and str(extra.get("agent_core_asset_lock_id") or "") == str(core_lock.id)
        and int(extra.get("agent_core_asset_lock_version") or 0) == core_lock.version
    )


def storyboard_quality_issues(
    production: AgentProduction,
    chapters: List[ProjectChapter],
    storyboards: List[ProjectStoryboard],
) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    if not storyboards:
        return [
            {
                "severity": "error",
                "code": "no_storyboards",
                "message": "目标剧集没有生成有效分镜。",
            }
        ]
    for storyboard in storyboards:
        storyboard_status = str(
            (storyboard.extra or {}).get("agent_storyboard_status") or "ready"
        )
        if storyboard_status != "ready":
            issues.append(
                {
                    "severity": "error",
                    "code": "storyboard_not_ready",
                    "message": f"分镜“{storyboard.title}”尚未填写完整或时长不合规。",
                    "chapter_id": str(storyboard.chapter_id),
                    "storyboard_id": str(storyboard.id),
                }
            )
        unbound = (storyboard.extra or {}).get("agent_unbound_asset_names") or {}
        for asset_type in ("character", "scene"):
            names = unbound.get(asset_type) or []
            if names:
                issues.append(
                    {
                        "severity": "error",
                        "code": f"unbound_{asset_type}",
                        "message": f"分镜“{storyboard.title}”引用了未锁定的{_asset_label(asset_type)}：{'、'.join(names)}。",
                        "chapter_id": str(storyboard.chapter_id),
                        "storyboard_id": str(storyboard.id),
                    }
                )
        prop_names = unbound.get("prop") or []
        if prop_names:
            issues.append(
                {
                    "severity": "warning",
                    "code": "unbound_prop",
                    "message": f"分镜“{storyboard.title}”包含未锁定道具：{'、'.join(prop_names)}。",
                    "chapter_id": str(storyboard.chapter_id),
                    "storyboard_id": str(storyboard.id),
                }
            )
        if not (storyboard.source_content or "").strip() or not (storyboard.action or "").strip():
            issues.append(
                {
                    "severity": "error",
                    "code": "incomplete_storyboard",
                    "message": f"分镜“{storyboard.title}”缺少来源文本或核心动作。",
                    "chapter_id": str(storyboard.chapter_id),
                    "storyboard_id": str(storyboard.id),
                }
            )
    target_seconds = sum(
        int((chapter.extra or {}).get("estimated_duration_seconds") or 0) for chapter in chapters
    )
    default_seconds = int(
        (production.production_spec or {}).get("default_shot_duration_seconds") or 5
    )
    estimated_seconds = sum(
        storyboard_estimated_duration_seconds(storyboard, default_seconds)
        for storyboard in storyboards
    )
    if target_seconds and abs(estimated_seconds - target_seconds) / target_seconds > 0.3:
        issues.append(
            {
                "severity": "warning",
                "code": "duration_deviation",
                "message": f"按分镜预估时长合计为 {estimated_seconds} 秒，与目标 {target_seconds} 秒偏差超过 30%。",
            }
        )
    return issues


def storyboard_asset_ids(storyboard: ProjectStoryboard) -> Dict[str, List[UUID]]:
    mapping = (storyboard.extra or {}).get("agent_asset_ids") or {}
    return {
        asset_type: [
            value
            for value in (_optional_uuid(item) for item in mapping.get(asset_type) or [])
            if value is not None
        ]
        for asset_type in ASSET_MODELS
    }


def storyboard_estimated_duration_seconds(
    storyboard: ProjectStoryboard,
    default_seconds: int = 1,
) -> int:
    value = (storyboard.extra or {}).get("estimated_duration_seconds")
    try:
        seconds = round(float(value))
    except (TypeError, ValueError):
        match = re.search(r"\d+(?:\.\d+)?", str(storyboard.duration_suggestion or ""))
        seconds = round(float(match.group(0))) if match else default_seconds
    return seconds if seconds > 0 else default_seconds


def _normalize_name(value: Any) -> str:
    return re.sub(r"[\s·•・_\-—]+", "", str(value or "").strip().lower())


def _asset_label(asset_type: str) -> str:
    return {"character": "角色", "scene": "场景", "prop": "道具"}.get(asset_type, "资产")


def _optional_uuid(value: Any) -> Optional[UUID]:
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None
