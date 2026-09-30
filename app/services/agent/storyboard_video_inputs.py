from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException
from app.models.agent_production import AgentProduction
from app.models.agent_story_bible import AgentAssetVariant
from app.models.project_asset import ProjectCharacter, ProjectProp, ProjectScene
from app.models.project_storyboard import ProjectStoryboard
from app.services.agent.storyboard_prompts import asset_token_keys, render_asset_tokens
from app.services.projects.storyboard_videos import resolve_storyboard_reference_image_urls


ASSET_MODELS = {
    "character": ProjectCharacter,
    "scene": ProjectScene,
    "prop": ProjectProp,
}
REFERENCE_ROLES = {
    "character": "character_identity",
    "scene": "scene_environment",
    "prop": "prop_appearance",
}
REFERENCE_INSTRUCTIONS = {
    "character": (
        "作为人物“{label}”的当前形象参考，保持五官、发型、服装、体型和身份一致；"
        "只参考人物形象，不得复刻白底、拼版或三视图布局。"
    ),
    "scene": (
        "作为场景“{label}”的环境参考，保持空间结构、材质、灯光、时间状态和氛围一致。"
    ),
    "prop": (
        "作为道具“{label}”的外观参考，保持完整结构、颜色、材质和关键细节一致；"
        "只参考道具外观，不得复刻白底、拼版或细节展示布局。"
    ),
}


@dataclass(frozen=True)
class AgentStoryboardVideoInput:
    prompt: str
    reference_images: List[str]
    reference_manifest: List[Dict[str, Any]]


async def build_agent_storyboard_video_input(
    db: AsyncSession,
    production: AgentProduction,
    storyboard: ProjectStoryboard,
    *,
    max_images: Optional[int],
    prompt_notes: Optional[str] = None,
) -> AgentStoryboardVideoInput:
    extra = storyboard.extra or {}
    bindings = [
        item
        for item in extra.get("agent_asset_bindings") or []
        if isinstance(item, dict)
    ]
    template = str(
        extra.get("agent_storyboard_prompt_template")
        or extra.get("agent_storyboard_prompt")
        or storyboard.video_prompt
        or ""
    ).strip()
    mentioned_keys = asset_token_keys(template)
    if mentioned_keys:
        bindings_by_key = {
            str(item.get("binding_key") or ""): item
            for item in bindings
            if str(item.get("binding_key") or "")
        }
        invalid_keys = [key for key in mentioned_keys if key not in bindings_by_key]
        if invalid_keys:
            raise AppException(
                "分镜提示词引用了未绑定的资产",
                code=40962,
                status_code=409,
                data={"invalid_binding_keys": invalid_keys},
            )
        bindings = [bindings_by_key[key] for key in mentioned_keys]
    if not bindings:
        raise AppException(
            "当前分镜组没有可用于多模态视频生成的资产绑定",
            code=40994,
            status_code=409,
        )

    assets = await _load_assets(db, production, bindings)
    variants = await _load_variants(db, production, bindings)
    labels = {
        str(key): str(value)
        for key, value in (extra.get("agent_asset_binding_labels") or {}).items()
        if str(key) and str(value)
    }
    references: List[Dict[str, Any]] = []
    missing: List[Dict[str, str]] = []
    for binding in bindings:
        asset_type = str(binding.get("asset_type") or "")
        binding_key = str(binding.get("binding_key") or "")
        asset_id = _optional_uuid(binding.get("asset_id"))
        variant_id = _optional_uuid(binding.get("variant_id"))
        if asset_type not in ASSET_MODELS or not binding_key or asset_id is None:
            continue
        asset = assets[asset_type].get(asset_id)
        variant = variants.get(variant_id) if variant_id is not None else None
        if variant is not None and variant.asset_type != asset_type:
            variant = None
        label = labels.get(binding_key) or (
            str(variant.canonical_name) if variant is not None else str(asset.name if asset else "")
        )
        if label:
            labels[binding_key] = label
        image_url = ""
        if variant_id is not None and variant is not None:
            image_url = str(variant.reference_image or "").strip()
        elif variant_id is None and asset is not None:
            image_url = str(asset.reference_image or "").strip()
        if not image_url:
            missing.append(
                {
                    "binding_key": binding_key,
                    "asset_type": asset_type,
                    "asset_id": str(asset_id),
                    "variant_id": str(variant_id) if variant_id is not None else "",
                    "label": label,
                }
            )
            continue
        references.append(
            {
                "binding_key": binding_key,
                "asset_type": asset_type,
                "asset_id": str(asset_id),
                "variant_id": str(variant_id) if variant_id is not None else None,
                "label": label,
                "reference_role": REFERENCE_ROLES[asset_type],
                "source_url": image_url,
            }
        )

    if missing:
        raise AppException(
            "分镜组存在缺少参考图的绑定资产或资产变体",
            code=40994,
            status_code=409,
            data={"missing_references": missing},
        )

    source_urls = list(dict.fromkeys(item["source_url"] for item in references))
    resolved_urls = await resolve_storyboard_reference_image_urls(db, source_urls)
    resolved_by_source = dict(zip(source_urls, resolved_urls))
    manifest: List[Dict[str, Any]] = []
    manifest_by_url: Dict[str, Dict[str, Any]] = {}
    binding_tokens: Dict[str, str] = {}
    for reference in references:
        image_url = resolved_by_source.get(reference["source_url"], reference["source_url"])
        item = manifest_by_url.get(image_url)
        if item is None:
            index = len(manifest) + 1
            item = {
                "index": index,
                "reference_token": f"@图片{index}",
                "media_type": "image",
                "reference_role": reference["reference_role"],
                "binding_key": reference["binding_key"],
                "binding_keys": [reference["binding_key"]],
                "asset_type": reference["asset_type"],
                "asset_id": reference["asset_id"],
                "variant_id": reference["variant_id"],
                "label": reference["label"],
                "url": image_url,
            }
            manifest.append(item)
            manifest_by_url[image_url] = item
        elif reference["binding_key"] not in item["binding_keys"]:
            item["binding_keys"].append(reference["binding_key"])
        binding_tokens[reference["binding_key"]] = item["reference_token"]

    if max_images is not None and len(manifest) > max_images:
        raise AppException(
            f"当前分镜组需要引用 {len(manifest)} 张图片，但所选模型最多支持 {max_images} 张",
            code=40037,
            status_code=400,
            data={
                "required_count": len(manifest),
                "max_count": max_images,
                "asset_bindings": manifest,
            },
        )

    prompt = _compile_prompt(storyboard, labels, binding_tokens, manifest, prompt_notes)
    return AgentStoryboardVideoInput(
        prompt=prompt,
        reference_images=[item["url"] for item in manifest],
        reference_manifest=manifest,
    )


async def _load_assets(
    db: AsyncSession,
    production: AgentProduction,
    bindings: List[Dict[str, Any]],
) -> Dict[str, Dict[UUID, Any]]:
    result: Dict[str, Dict[UUID, Any]] = {key: {} for key in ASSET_MODELS}
    for asset_type, model in ASSET_MODELS.items():
        ids = {
            value
            for value in (
                _optional_uuid(item.get("asset_id"))
                for item in bindings
                if item.get("asset_type") == asset_type
            )
            if value is not None
        }
        if not ids:
            continue
        rows = await db.execute(
            select(model).where(
                model.id.in_(ids),
                model.project_id == production.project_id,
                model.user_id == production.user_id,
                model.is_enabled.is_(True),
            )
        )
        result[asset_type] = {item.id: item for item in rows.scalars().all()}
    return result


async def _load_variants(
    db: AsyncSession,
    production: AgentProduction,
    bindings: List[Dict[str, Any]],
) -> Dict[UUID, AgentAssetVariant]:
    ids = {
        value
        for value in (_optional_uuid(item.get("variant_id")) for item in bindings)
        if value is not None
    }
    if not ids:
        return {}
    result = await db.execute(
        select(AgentAssetVariant).where(
            AgentAssetVariant.id.in_(ids),
            AgentAssetVariant.production_id == production.id,
            AgentAssetVariant.project_id == production.project_id,
            AgentAssetVariant.user_id == production.user_id,
            AgentAssetVariant.review_status == "ready",
        )
    )
    return {item.id: item for item in result.scalars().all()}


def _compile_prompt(
    storyboard: ProjectStoryboard,
    labels: Dict[str, str],
    binding_tokens: Dict[str, str],
    manifest: List[Dict[str, Any]],
    prompt_notes: Optional[str],
) -> str:
    template = str(
        (storyboard.extra or {}).get("agent_storyboard_prompt_template")
        or (storyboard.extra or {}).get("agent_storyboard_prompt")
        or storyboard.video_prompt
        or ""
    ).strip()
    tokenized_labels = {
        key: f"{label}（参考{binding_tokens[key]}）" if key in binding_tokens else label
        for key, label in labels.items()
    }
    storyboard_prompt = render_asset_tokens(template, tokenized_labels)
    reference_lines = ["多模态参考要求："]
    for item in manifest:
        instruction = REFERENCE_INSTRUCTIONS[item["asset_type"]].format(label=item["label"])
        reference_lines.append(f'{item["reference_token"]} {instruction}')
    parts = [storyboard_prompt, "\n".join(reference_lines)]
    if prompt_notes and prompt_notes.strip():
        parts.append(
            f"用户补充要求：{prompt_notes.strip()}\n"
            "用户补充要求只能调整当前分镜组的动作强度、节奏、氛围、运镜或画面质感，"
            "不得覆盖分镜剧情、资产绑定、多模态参考、总时长和固定禁用规则。"
        )
    parts.append(
        "多模态参考图只用于保持资产外观一致，不得把参考图中的白底、拼版、三视图、"
        "细节展示布局、说明文字或水印生成到视频画面中。"
    )
    return "\n\n".join(part for part in parts if part)


def _optional_uuid(value: Any) -> Optional[UUID]:
    try:
        return UUID(str(value)) if value else None
    except (TypeError, ValueError, AttributeError):
        return None
