import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException
from app.core.timezone import beijing_datetime
from app.models.agent_core_asset import AgentCoreAssetLock
from app.models.agent_production import AgentCheckpoint, AgentEvent, AgentProduction, AgentStep
from app.models.agent_story_bible import (
    AGENT_ASSET_VARIANT_TYPES,
    AgentAssetCandidate,
    AgentAssetVariant,
    SeriesBibleVersion,
)
from app.models.project import Project
from app.models.project_asset import ProjectCharacter, ProjectProp, ProjectScene
from app.models.project_generated_asset import ProjectGeneratedAsset
from app.models.project_storyboard import ProjectStoryboard
from app.models.project_chapter import ProjectChapter
from app.models.user import User
from app.schemas.agent_core_asset import (
    CoreAssetConfirmRequest,
    CoreAssetCreateRequest,
    CoreAssetImageGenerationRequest,
    CoreAssetLockRequest,
    CoreAssetRef,
    CoreAssetReferenceImageRequest,
    CoreAssetSelectionRequest,
    CoreAssetUpdateRequest,
    CoreAssetVariantCreateRequest,
    CoreAssetVariantImageGenerationRequest,
)
from app.schemas.agent_storyboard import AgentStoryboardGenerateRequest
from app.schemas.project_asset import ProjectAssetImageGenerateRequest
from app.services.agent_default_models import resolve_fixed_agent_models
from app.services.agent_workflow import agent_pilot_episode_count, uses_agent_workflow_v2
from app.services.core_asset_change_tracking import track_core_asset_reference_change
from app.services.model_points import calculate_submission_points_cost
from app.services.points import change_user_points, consume_user_points, ensure_user_points_enough
from app.services.provider_polling import provider_next_poll_seconds
from app.services.project_asset_generation import (
    _validate_comfly_asset_image_request,
    build_asset_image_prompt,
    get_enabled_image_model_or_404,
    get_project_with_style_or_404,
    submit_asset_image_generation,
)
from app.services.task_records import create_user_task_record


ASSET_MODELS = {
    "character": ProjectCharacter,
    "scene": ProjectScene,
    "prop": ProjectProp,
}
CORE_ASSET_IMAGE_ASPECT_RATIO = "16:9"


@dataclass
class CoreAssetEntry:
    candidate: AgentAssetCandidate
    asset: Optional[Any]


@dataclass
class CoreAssetContext:
    production: AgentProduction
    bible: SeriesBibleVersion
    entries: Dict[Tuple[str, UUID], CoreAssetEntry]
    active_lock: Optional[AgentCoreAssetLock]


async def get_core_asset_readiness(
    db: AsyncSession,
    production_id: UUID,
    user_id: UUID,
) -> Dict[str, Any]:
    context = await _get_context(db, production_id, user_id, lock=False)
    locked_assets = _snapshot_map((context.active_lock.assets if context.active_lock else []) or [])
    items: List[Dict[str, Any]] = []
    available_types = set()
    missing_reference_count = 0
    for key, entry in sorted(
        context.entries.items(), key=lambda item: (item[0][0], item[1].candidate.canonical_name)
    ):
        asset_type, asset_id = key
        asset = entry.asset
        reference_image = asset.reference_image if asset is not None else None
        if asset is not None:
            available_types.add(asset_type)
        if not reference_image:
            missing_reference_count += 1
        locked_snapshot = locked_assets.get(key)
        items.append(
            {
                "asset_type": asset_type,
                "asset_id": asset_id,
                "candidate_id": entry.candidate.id,
                "name": asset.name if asset is not None else entry.candidate.canonical_name,
                "aliases": list(entry.candidate.aliases or []),
                "reference_image": reference_image,
                "review_status": entry.candidate.review_status,
                "image_generation_status": (
                    (asset.extra or {}).get("image_generation_status")
                    if asset is not None
                    else None
                ),
                "locked": bool(
                    locked_snapshot and locked_snapshot.get("reference_image") == reference_image
                ),
            }
        )
    ready_count = len(items) - missing_reference_count
    return {
        "bible_version": context.bible.version,
        "lock_version": context.active_lock.version if context.active_lock else 0,
        "lock_status": context.active_lock.status if context.active_lock else None,
        "items": items,
        "ready_count": ready_count,
        "missing_reference_count": missing_reference_count,
        "can_lock": {"character", "scene"}.issubset(available_types),
    }


async def list_core_assets(
    db: AsyncSession,
    production_id: UUID,
    user_id: UUID,
    *,
    asset_type: Optional[str],
    keyword: Optional[str],
    page: int,
    page_size: int,
) -> Dict[str, Any]:
    context = await _get_context(db, production_id, user_id, lock=False)
    entries = [
        entry
        for (entry_type, _asset_id), entry in context.entries.items()
        if entry.asset is not None and (asset_type is None or entry_type == asset_type)
    ]
    if keyword:
        normalized = keyword.strip().lower()
        entries = [entry for entry in entries if _entry_matches_keyword(entry, normalized)]
    entries.sort(key=lambda entry: (entry.candidate.asset_type, entry.candidate.canonical_name))
    total = len(entries)
    selected = entries[(page - 1) * page_size : page * page_size]
    variants = await _variants_by_candidate_ids(
        db,
        [entry.candidate.id for entry in selected],
    )
    return {
        "items": [
            _managed_asset_payload(entry, variants.get(entry.candidate.id, []))
            for entry in selected
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


async def get_core_asset(
    db: AsyncSession,
    production_id: UUID,
    user_id: UUID,
    asset_type: str,
    asset_id: UUID,
) -> Dict[str, Any]:
    context = await _get_context(db, production_id, user_id, lock=False)
    entry = _managed_entry(context, asset_type, asset_id)
    variants = await _variants_by_candidate_ids(db, [entry.candidate.id])
    return _managed_asset_payload(entry, variants.get(entry.candidate.id, []))


async def create_core_asset(
    db: AsyncSession,
    production_id: UUID,
    user_id: UUID,
    payload: CoreAssetCreateRequest,
) -> Dict[str, Any]:
    context = await _get_context(db, production_id, user_id, lock=True)
    _require_core_asset_editable(context)
    asset = _new_managed_asset(context.production, payload)
    db.add(asset)
    await db.flush()
    candidate = AgentAssetCandidate(
        project_id=context.production.project_id,
        production_id=context.production.id,
        bible_version_id=context.bible.id,
        user_id=user_id,
        asset_type=payload.asset_type,
        candidate_key=hashlib.sha256(f"manual:{uuid4()}".encode()).hexdigest(),
        canonical_name=payload.canonical_name,
        aliases=payload.aliases,
        source_chapter_ids=[],
        confidence=1,
        merge_reason="用户在核心资产阶段手动创建",
        review_status="materialized",
        content=payload.content,
        materialized_asset_id=asset.id,
        lock_version=0,
    )
    db.add(candidate)
    await db.flush()
    asset.extra = {
        **(asset.extra or {}),
        "agent_production_id": str(context.production.id),
        "agent_candidate_ids": [str(candidate.id)],
    }
    context.production.lock_version += 1
    await db.commit()
    await db.refresh(asset)
    await db.refresh(candidate)
    return _managed_asset_payload(CoreAssetEntry(candidate, asset), [])


async def update_core_asset(
    db: AsyncSession,
    production_id: UUID,
    user_id: UUID,
    asset_type: str,
    asset_id: UUID,
    payload: CoreAssetUpdateRequest,
) -> Dict[str, Any]:
    context = await _get_context(db, production_id, user_id, lock=True)
    _require_core_asset_editable(context)
    entry = _managed_entry(context, asset_type, asset_id)
    candidate = entry.candidate
    asset = entry.asset
    if candidate.lock_version != payload.expected_lock_version:
        raise AppException(
            f"核心资产版本冲突，当前版本为 {candidate.lock_version}",
            code=40933,
            status_code=409,
            data={"current_lock_version": candidate.lock_version},
        )
    if payload.canonical_name is not None:
        candidate.canonical_name = payload.canonical_name
    if payload.aliases is not None:
        candidate.aliases = payload.aliases
    if payload.content is not None:
        candidate.content = {**(candidate.content or {}), **payload.content}
    _sync_managed_asset(asset, candidate)
    candidate.lock_version += 1
    candidate.updated_at = beijing_datetime()
    asset.updated_at = beijing_datetime()
    context.production.lock_version += 1
    await db.commit()
    await db.refresh(asset)
    await db.refresh(candidate)
    variants = await _variants_by_candidate_ids(db, [candidate.id])
    return _managed_asset_payload(entry, variants.get(candidate.id, []))


async def delete_core_asset(
    db: AsyncSession,
    production_id: UUID,
    user_id: UUID,
    asset_type: str,
    asset_id: UUID,
    expected_lock_version: int,
) -> Dict[str, Any]:
    context = await _get_context(db, production_id, user_id, lock=True)
    _require_core_asset_editable(context)
    entry = _managed_entry(context, asset_type, asset_id)
    candidate = entry.candidate
    asset = entry.asset
    if candidate.lock_version != expected_lock_version:
        raise AppException(
            f"核心资产版本冲突，当前版本为 {candidate.lock_version}",
            code=40933,
            status_code=409,
            data={"current_lock_version": candidate.lock_version},
        )
    if _has_active_image_task(asset):
        raise AppException("核心资产正在生成参考图，暂不能删除", code=40939, status_code=409)
    variant_result = await db.execute(
        select(AgentAssetVariant)
        .where(AgentAssetVariant.base_candidate_id == candidate.id)
        .with_for_update(of=AgentAssetVariant)
    )
    variants = list(variant_result.scalars().all())
    if any(_has_active_variant_image_task(variant) for variant in variants):
        raise AppException("资产变体正在生成参考图，暂不能删除基础资产", code=40939, status_code=409)
    for variant in variants:
        await db.delete(variant)
    candidate.review_status = "rejected"
    candidate.materialized_asset_id = None
    candidate.lock_version += 1
    candidate.updated_at = beijing_datetime()
    asset.is_enabled = False
    asset.updated_at = beijing_datetime()
    context.production.lock_version += 1
    await db.commit()
    return {
        "asset_type": asset_type,
        "asset_id": asset_id,
        "deleted": True,
        "deleted_variant_count": len(variants),
    }


async def create_core_asset_variant(
    db: AsyncSession,
    production_id: UUID,
    user_id: UUID,
    asset_type: str,
    asset_id: UUID,
    payload: CoreAssetVariantCreateRequest,
) -> AgentAssetVariant:
    context = await _get_context(db, production_id, user_id, lock=True)
    _require_core_asset_editable(context)
    entry = _managed_entry(context, asset_type, asset_id)
    variant_type = payload.variant_type.strip().lower()
    if variant_type not in AGENT_ASSET_VARIANT_TYPES[asset_type]:
        raise AppException("资产变体类型无效", code=40061, status_code=400)
    variant_key = hashlib.sha256(
        (
            f"{asset_type}:{entry.candidate.candidate_key}:"
            f"{_normalize_name(variant_type)}:{_normalize_name(payload.canonical_name)}"
        ).encode("utf-8")
    ).hexdigest()
    existing_result = await db.execute(
        select(AgentAssetVariant.id).where(
            AgentAssetVariant.bible_version_id == context.bible.id,
            AgentAssetVariant.base_candidate_id == entry.candidate.id,
            AgentAssetVariant.variant_key == variant_key,
        )
    )
    if existing_result.scalar_one_or_none() is not None:
        raise AppException("相同名称和类型的资产变体已存在", code=40938, status_code=409)
    variant = AgentAssetVariant(
        project_id=context.production.project_id,
        production_id=context.production.id,
        bible_version_id=context.bible.id,
        base_candidate_id=entry.candidate.id,
        user_id=user_id,
        asset_type=asset_type,
        variant_key=variant_key,
        canonical_name=payload.canonical_name.strip(),
        variant_type=variant_type,
        description=payload.description.strip(),
        trigger_reason=payload.trigger_reason.strip(),
        episode_numbers=payload.episode_numbers,
        source_evidence=[item.model_dump() for item in payload.source_evidence],
        confidence=1,
        review_status="ready",
        content=payload.content,
        lock_version=0,
    )
    db.add(variant)
    entry.candidate.lock_version += 1
    entry.candidate.updated_at = beijing_datetime()
    context.production.lock_version += 1
    await db.commit()
    await db.refresh(variant)
    return variant


async def delete_core_asset_variant(
    db: AsyncSession,
    production_id: UUID,
    user_id: UUID,
    asset_type: str,
    asset_id: UUID,
    variant_id: UUID,
    expected_lock_version: int,
) -> Dict[str, Any]:
    context = await _get_context(db, production_id, user_id, lock=True)
    _require_core_asset_editable(context)
    entry = _managed_entry(context, asset_type, asset_id)
    result = await db.execute(
        select(AgentAssetVariant)
        .where(
            AgentAssetVariant.id == variant_id,
            AgentAssetVariant.base_candidate_id == entry.candidate.id,
            AgentAssetVariant.asset_type == asset_type,
        )
        .with_for_update(of=AgentAssetVariant)
    )
    variant = result.scalar_one_or_none()
    if variant is None:
        raise AppException("资产变体不存在", code=40440, status_code=404)
    if variant.lock_version != expected_lock_version:
        raise AppException(
            f"资产变体版本冲突，当前版本为 {variant.lock_version}",
            code=40940,
            status_code=409,
            data={"current_lock_version": variant.lock_version},
        )
    if _has_active_variant_image_task(variant):
        raise AppException("资产变体正在生成参考图，暂不能删除", code=40939, status_code=409)
    await db.delete(variant)
    entry.candidate.lock_version += 1
    entry.candidate.updated_at = beijing_datetime()
    context.production.lock_version += 1
    await db.commit()
    return {"variant_id": variant_id, "deleted": True}


async def update_core_asset_variant_reference_image(
    db: AsyncSession,
    production_id: UUID,
    user_id: UUID,
    asset_type: str,
    asset_id: UUID,
    variant_id: UUID,
    payload: CoreAssetReferenceImageRequest,
) -> AgentAssetVariant:
    context = await _get_context(db, production_id, user_id, lock=True)
    _require_core_asset_image_editable(context)
    entry = _managed_entry(context, asset_type, asset_id)
    variant = await _managed_variant(db, entry, asset_type, variant_id, lock=True)
    if variant.lock_version != payload.expected_lock_version:
        raise AppException(
            f"资产变体版本冲突，当前版本为 {variant.lock_version}",
            code=40940,
            status_code=409,
            data={"current_lock_version": variant.lock_version},
        )
    if _has_active_variant_image_task(variant):
        raise AppException("资产变体正在生成参考图，暂不能替换", code=40939, status_code=409)

    await track_core_asset_reference_change(
        db,
        project_id=context.production.project_id,
        user_id=user_id,
        asset_type=asset_type,
        asset_id=asset_id,
        variant_id=variant.id,
        previous_reference_image=variant.reference_image,
        new_reference_image=payload.reference_image,
    )
    variant.reference_image = payload.reference_image
    variant.extra = {
        **(variant.extra or {}),
        "image_generation_status": "selected" if payload.reference_image else None,
        "reference_image_source": "manual_upload" if payload.reference_image else None,
    }
    variant.lock_version += 1
    variant.updated_at = beijing_datetime()
    context.production.lock_version += 1
    await db.commit()
    await db.refresh(variant)
    return variant


async def submit_core_asset_variant_image_generation(
    db: AsyncSession,
    production_id: UUID,
    user: User,
    asset_type: str,
    asset_id: UUID,
    variant_id: UUID,
    payload: CoreAssetVariantImageGenerationRequest,
) -> Dict[str, Any]:
    context = await _get_context(db, production_id, user.id, lock=True)
    _require_core_asset_image_editable(context)
    entry = _managed_entry(context, asset_type, asset_id)
    variant = await _managed_variant(db, entry, asset_type, variant_id, lock=True)
    base_reference_image = entry.asset.reference_image
    if not base_reference_image:
        raise AppException("请先设置基础资产主图", code=40984, status_code=409)
    if _has_active_variant_image_task(variant):
        return {
            "asset_type": asset_type,
            "asset_id": asset_id,
            "variant_id": variant.id,
            "submitted": False,
            "task_record_id": _optional_uuid(
                (variant.extra or {}).get("image_generation_task_record_id")
            ),
            "status": (variant.extra or {}).get("image_generation_status"),
            "points_cost": 0,
        }

    ai_model = (await resolve_fixed_agent_models(db, ("image",)))["image"]
    project = await get_project_with_style_or_404(
        db,
        context.production.project_id,
        user.id,
    )
    prompt = build_asset_variant_image_prompt(
        project,
        entry.asset,
        variant,
        asset_type,
        payload.prompt,
    )
    model_extra = {
        **(payload.extra or {}),
        "aspect_ratio": CORE_ASSET_IMAGE_ASPECT_RATIO,
        "generation_mode": "general",
        "images": [base_reference_image],
        "image_urls": [base_reference_image],
    }
    _validate_comfly_asset_image_request(ai_model, prompt, model_extra)
    points_cost = calculate_submission_points_cost(ai_model, "image", model_extra)
    await ensure_user_points_enough(db, user.id, points_cost)
    transaction = None
    if points_cost > 0:
        transaction = await consume_user_points(
            db,
            user_id=user.id,
            amount=points_cost,
            remark=f"资产变体图像生成：{variant.canonical_name}",
            auto_commit=False,
        )
    task_record = await create_user_task_record(
        db,
        user_id=user.id,
        ai_model_id=ai_model.id,
        points_transaction_id=transaction.id if transaction else None,
        business_type="project",
        business_id=context.production.project_id,
        generation_type="asset_image_generate",
        status="pending",
        title=f"资产变体图像生成：{variant.canonical_name}",
        prompt=prompt,
        points_cost=points_cost,
        extra={
            "project_id": str(context.production.project_id),
            "asset_type": asset_type,
            "asset_id": str(asset_id),
            "asset_name": entry.asset.name,
            "agent_asset_variant_id": str(variant.id),
            "variant_name": variant.canonical_name,
            "generation_mode": "general",
            "generation_ratio": CORE_ASSET_IMAGE_ASPECT_RATIO,
            "model_extra": model_extra,
        },
    )
    await db.flush()
    variant.extra = {
        **(variant.extra or {}),
        "image_generation_status": "pending",
        "image_generation_task_record_id": str(task_record.id),
    }
    context.production.estimated_points += points_cost
    context.production.consumed_points += points_cost
    context.production.lock_version += 1
    await db.commit()

    try:
        from app.tasks.project_asset_generation import run_project_asset_image_generation

        run_project_asset_image_generation.apply_async(
            args=(str(task_record.id), asset_type, str(asset_id)),
            queue="story_ai_image",
            routing_key="story_ai_image",
        )
    except Exception:
        await _fail_variant_image_enqueue(
            db,
            context,
            variant,
            task_record,
            points_cost,
            user,
        )

    return {
        "asset_type": asset_type,
        "asset_id": asset_id,
        "variant_id": variant.id,
        "submitted": task_record.status != "failed",
        "task_record_id": task_record.id,
        "status": task_record.status,
        "points_cost": points_cost if task_record.status != "failed" else 0,
        "next_poll_seconds": (
            provider_next_poll_seconds(
                task_record.generation_type,
                task_record.status,
                task_record.extra or {},
            )
            if task_record.status != "failed"
            else None
        ),
    }


async def _fail_variant_image_enqueue(
    db: AsyncSession,
    context: CoreAssetContext,
    variant: AgentAssetVariant,
    task_record: Any,
    points_cost: int,
    user: User,
) -> None:
    refund_transaction_id = None
    if points_cost > 0:
        refund = await change_user_points(
            db,
            user_id=user.id,
            amount=points_cost,
            transaction_type="refund",
            remark=f"任务入队失败退回积分：{task_record.title}",
            auto_commit=False,
        )
        refund_transaction_id = str(refund.id)
    task_record.status = "failed"
    task_record.result = "任务入队失败"
    task_record.extra = {
        **(task_record.extra or {}),
        "failed_reason": "任务入队失败",
        "refund_transaction_id": refund_transaction_id,
    }
    variant.extra = {
        **(variant.extra or {}),
        "image_generation_status": "failed",
        "image_generation_failed_reason": "任务入队失败",
    }
    context.production.consumed_points = max(
        0,
        context.production.consumed_points - points_cost,
    )
    context.production.estimated_points = max(
        0,
        context.production.estimated_points - points_cost,
    )
    await db.commit()


def build_asset_variant_image_prompt(
    project: Project,
    base_asset: Any,
    variant: AgentAssetVariant,
    asset_type: str,
    custom_prompt: Optional[str] = None,
) -> str:
    content = variant.content or {}
    visual_delta = json.dumps(content.get("visual_delta") or {}, ensure_ascii=False)
    preserve_anchors = "、".join(
        str(item).strip()
        for item in content.get("preserve_anchors") or []
        if str(item).strip()
    )
    base_prompt = build_asset_image_prompt(
        project,
        base_asset,
        asset_type,
        "general",
        custom_prompt,
    )
    return "\n".join(
        (
            base_prompt,
            "变体生成规则：必须继承基础资产参考图中的身份、结构、比例和固定视觉特征。",
            f"变体名称：{variant.canonical_name}",
            f"剧情变化：{variant.description}",
            f"触发原因：{variant.trigger_reason}",
            f"可见变化：{visual_delta}",
            f"必须保留：{preserve_anchors}",
            "只能应用上述可见变化，不得重新设计成另一个资产。",
        )
    )


async def submit_core_asset_image_generations(
    db: AsyncSession,
    production_id: UUID,
    user: User,
    payload: CoreAssetImageGenerationRequest,
) -> Dict[str, Any]:
    context = await _get_context(db, production_id, user.id, lock=True)
    if context.production.status not in {
        "planning",
        "running",
        "waiting_approval",
        "completed",
    }:
        raise AppException("当前整剧状态不允许生成核心资产参考图", code=40932, status_code=409)
    requested_entries = _requested_entries(context, payload.items)
    if uses_agent_workflow_v2(context.production):
        ai_model = (await resolve_fixed_agent_models(db, ("image",)))["image"]
    elif payload.ai_model_id is None:
        ai_model = (await resolve_fixed_agent_models(db, ("image",)))["image"]
    else:
        ai_model = await get_enabled_image_model_or_404(db, payload.ai_model_id)
    estimated_cost = sum(
        calculate_submission_points_cost(ai_model, "image", item.extra or {})
        for item, _entry in requested_entries
        if _entry.asset is not None and not _has_active_image_task(_entry.asset)
    )
    if (
        context.production.max_points is not None
        and context.production.consumed_points + estimated_cost > context.production.max_points
    ):
        raise AppException("整剧任务已达到积分预算上限", code=40052, status_code=400)
    await ensure_user_points_enough(db, user.id, estimated_cost)

    results: List[Dict[str, Any]] = []
    submitted_count = 0
    failed_count = 0
    total_points_cost = 0
    for item, entry in requested_entries:
        asset = entry.asset
        if asset is None:
            results.append(
                _generation_error(item.asset_type, item.asset_id, 40434, "核心资产不存在")
            )
            failed_count += 1
            continue
        if _has_active_image_task(asset):
            results.append(
                {
                    "asset_type": item.asset_type,
                    "asset_id": item.asset_id,
                    "submitted": False,
                    "task_record_id": _optional_uuid(
                        (asset.extra or {}).get("image_generation_task_record_id")
                    ),
                    "status": (asset.extra or {}).get("image_generation_status"),
                    "points_cost": 0,
                }
            )
            continue
        try:
            _asset, task_record, points_cost = await submit_asset_image_generation(
                db,
                context.production.project_id,
                item.asset_type,
                item.asset_id,
                user,
                ProjectAssetImageGenerateRequest(
                    ai_model_id=ai_model.id,
                    generation_mode=item.generation_mode,
                    prompt=item.prompt,
                    extra=item.extra,
                ),
                aspect_ratio=CORE_ASSET_IMAGE_ASPECT_RATIO,
            )
        except AppException as exc:
            results.append(_generation_error(item.asset_type, item.asset_id, exc.code, exc.message))
            failed_count += 1
            continue
        if task_record.status == "failed":
            results.append(
                {
                    "asset_type": item.asset_type,
                    "asset_id": item.asset_id,
                    "submitted": False,
                    "task_record_id": task_record.id,
                    "status": task_record.status,
                    "points_cost": 0,
                    "error_code": 50301,
                    "error_message": task_record.result or "任务入队失败",
                }
            )
            failed_count += 1
            continue
        await db.execute(
            update(AgentProduction)
            .where(AgentProduction.id == context.production.id)
            .values(
                estimated_points=AgentProduction.estimated_points + points_cost,
                consumed_points=AgentProduction.consumed_points + points_cost,
                lock_version=AgentProduction.lock_version + 1,
            )
        )
        await db.commit()
        submitted_count += 1
        total_points_cost += points_cost
        results.append(
            {
                "asset_type": item.asset_type,
                "asset_id": item.asset_id,
                "submitted": True,
                "task_record_id": task_record.id,
                "status": task_record.status,
                "points_cost": points_cost,
                "next_poll_seconds": provider_next_poll_seconds(
                    task_record.generation_type,
                    task_record.status,
                    task_record.extra or {},
                ),
            }
        )
    return {
        "items": results,
        "submitted_count": submitted_count,
        "failed_count": failed_count,
        "total_points_cost": total_points_cost,
    }


async def update_core_asset_reference_image(
    db: AsyncSession,
    production_id: UUID,
    user_id: UUID,
    asset_type: str,
    asset_id: UUID,
    payload: CoreAssetReferenceImageRequest,
) -> Dict[str, Any]:
    context = await _get_context(db, production_id, user_id, lock=True)
    _require_core_asset_image_editable(context)
    entry = _managed_entry(context, asset_type, asset_id)
    candidate = entry.candidate
    asset = entry.asset
    if candidate.lock_version != payload.expected_lock_version:
        raise AppException(
            f"核心资产版本冲突，当前版本为 {candidate.lock_version}",
            code=40933,
            status_code=409,
            data={"current_lock_version": candidate.lock_version},
        )
    if _has_active_image_task(asset):
        raise AppException("核心资产正在生成参考图，暂不能替换", code=40939, status_code=409)

    await track_core_asset_reference_change(
        db,
        project_id=context.production.project_id,
        user_id=user_id,
        asset_type=asset_type,
        asset_id=asset_id,
        previous_reference_image=asset.reference_image,
        new_reference_image=payload.reference_image,
    )
    asset.reference_image = payload.reference_image
    asset.extra = {
        **(asset.extra or {}),
        "image_generation_status": "selected" if payload.reference_image else None,
        "reference_image_source": "manual_upload" if payload.reference_image else None,
    }
    candidate.lock_version += 1
    candidate.updated_at = beijing_datetime()
    asset.updated_at = beijing_datetime()
    context.production.lock_version += 1
    db.add(
        AgentEvent(
            production_id=context.production.id,
            actor_user_id=user_id,
            event_type="core_asset.reference_image_selected",
            source="user",
            payload={
                "asset_type": asset_type,
                "asset_id": str(asset_id),
                "reference_image": payload.reference_image,
            },
        )
    )
    await db.commit()
    await db.refresh(asset)
    await db.refresh(candidate)
    variants = await _variants_by_candidate_ids(db, [candidate.id])
    return _managed_asset_payload(entry, variants.get(candidate.id, []))


async def preview_core_asset_impact(
    db: AsyncSession,
    production_id: UUID,
    user_id: UUID,
    payload: CoreAssetSelectionRequest,
) -> Dict[str, Any]:
    context = await _get_context(db, production_id, user_id, lock=False)
    proposed = _selection_snapshot(context, payload.assets)
    proposed = await _attach_variant_snapshots(db, context, proposed)
    return await _build_impact(db, context, proposed)


async def lock_core_assets(
    db: AsyncSession,
    production_id: UUID,
    user: User,
    payload: Union[CoreAssetLockRequest, CoreAssetConfirmRequest],
    *,
    auto_select_all: bool = False,
) -> Dict[str, Any]:
    context = await _get_context(db, production_id, user.id, lock=True)
    existing_result = await db.execute(
        select(AgentCoreAssetLock).where(
            AgentCoreAssetLock.production_id == production_id,
            AgentCoreAssetLock.idempotency_key == payload.idempotency_key,
        )
    )
    existing = existing_result.scalar_one_or_none()
    if existing is not None:
        return _lock_result(existing, already_locked=True)

    if context.production.status in {"completed", "cancelled", "paused", "partially_failed"}:
        raise AppException("当前整剧状态不允许锁定核心资产", code=40932, status_code=409)
    if context.active_lock is None and context.production.current_stage != "core_assets":
        raise AppException("当前整剧阶段尚未进入核心资产确认", code=40932, status_code=409)

    current_version = context.active_lock.version if context.active_lock else 0
    if payload.expected_lock_version != current_version:
        raise AppException(
            f"核心资产锁版本冲突，当前版本为 {current_version}",
            code=40933,
            status_code=409,
            data={
                "expected_lock_version": payload.expected_lock_version,
                "current_lock_version": current_version,
            },
        )
    proposed = (
        _all_asset_snapshot(context)
        if auto_select_all
        else _selection_snapshot(context, payload.assets)
    )
    proposed = await _attach_variant_snapshots(db, context, proposed)
    impact = await _build_impact(db, context, proposed)
    if context.active_lock is not None:
        if (
            payload.impact_fingerprint is not None
            and payload.impact_fingerprint != impact["impact_fingerprint"]
        ) or (not auto_select_all and payload.impact_fingerprint is None):
            raise AppException(
                "核心资产影响预览已变化，请重新预览后确认",
                code=40935,
                status_code=409,
                data={"current_impact_fingerprint": impact["impact_fingerprint"]},
            )
        if not impact["changes"]:
            return _lock_result(context.active_lock, already_locked=True)

    version = current_version + 1
    now = beijing_datetime()
    step = AgentStep(
        production_id=context.production.id,
        stage="core_assets",
        scope_type="production",
        scope_id=context.production.id,
        status="completed",
        input_version=version,
        output_version=version,
        progress_current=len(proposed),
        progress_total=len(proposed),
        attempt_count=1,
        started_at=now,
        finished_at=now,
        extra={"asset_count": len(proposed)},
    )
    db.add(step)
    await db.flush()
    if context.active_lock is not None:
        context.active_lock.status = "superseded"
    lock_record = AgentCoreAssetLock(
        project_id=context.production.project_id,
        production_id=context.production.id,
        bible_version_id=context.bible.id,
        step_id=step.id,
        version=version,
        status="active",
        assets=proposed,
        impact=impact,
        idempotency_key=payload.idempotency_key,
        created_by=user.id,
    )
    db.add(lock_record)
    await db.flush()
    db.add(
        AgentCheckpoint(
            production_id=context.production.id,
            step_id=step.id,
            checkpoint_type="core_assets_review",
            status="approved",
            summary=f"已锁定 {len(proposed)} 个核心资产，版本 {version}。",
            impact=impact,
            approved_by=user.id,
            approved_at=now,
            extra={"core_asset_lock_id": str(lock_record.id)},
        )
    )
    await _invalidate_affected_outputs(
        db,
        impact["affected_storyboard_ids"],
        lock_record.id,
        version,
    )
    context.production.current_stage = (
        "pilot_production"
        if agent_pilot_episode_count(context.production) > 0
        else "batch_production"
    )
    production_extra = dict(context.production.extra or {})
    production_extra["core_asset_pending_changes"] = []
    production_extra.pop("core_asset_change_review_return_stage", None)
    context.production.extra = production_extra
    context.production.lock_version += 1
    db.add(
        AgentEvent(
            production_id=context.production.id,
            step_id=step.id,
            actor_user_id=user.id,
            event_type="core_assets.locked",
            source="user",
            payload={
                "lock_id": str(lock_record.id),
                "version": version,
                "asset_count": len(proposed),
                "impact_fingerprint": impact["impact_fingerprint"],
            },
        )
    )
    await db.commit()
    return _lock_result(lock_record, already_locked=False)


async def confirm_core_assets(
    db: AsyncSession,
    production_id: UUID,
    user: User,
    payload: CoreAssetConfirmRequest,
) -> Dict[str, Any]:
    result = await lock_core_assets(
        db,
        production_id,
        user,
        payload,
        auto_select_all=True,
    )
    production = await db.get(AgentProduction, production_id)
    if (
        production is not None
        and uses_agent_workflow_v2(production)
        and production.current_stage == "core_assets"
    ):
        production.current_stage = "batch_production"
        production.lock_version += 1
        await db.commit()
    if (
        production is not None
        and uses_agent_workflow_v2(production)
        and production.current_stage in {"batch_production", "batch_storyboards"}
    ):
        chapter_ids = await _pending_incremental_storyboard_chapter_ids(db, production)
        await _generate_storyboards_after_core_asset_confirmation(
            db,
            production_id,
            user,
            AgentStoryboardGenerateRequest(
                expected_core_asset_lock_version=int(result["version"]),
                idempotency_key=f"core-assets-{result['lock_id']}-storyboards",
            ),
            chapter_ids=chapter_ids,
        )
        if chapter_ids is not None:
            production_extra = dict(production.extra or {})
            production_extra.pop("pending_incremental_storyboard_chapter_ids", None)
            production_extra.pop("pending_incremental_storyboard_episode_numbers", None)
            production.extra = production_extra
            await db.commit()
    return result


async def _generate_storyboards_after_core_asset_confirmation(
    db: AsyncSession,
    production_id: UUID,
    user: User,
    payload: AgentStoryboardGenerateRequest,
    *,
    chapter_ids: Optional[List[UUID]] = None,
) -> None:
    from app.services.agent_storyboards import generate_agent_storyboards

    await generate_agent_storyboards(
        db,
        production_id,
        user,
        payload,
        chapter_ids=chapter_ids,
    )


async def _pending_incremental_storyboard_chapter_ids(
    db: AsyncSession,
    production: AgentProduction,
) -> Optional[List[UUID]]:
    raw_chapter_ids = (production.extra or {}).get(
        "pending_incremental_storyboard_chapter_ids"
    )
    if isinstance(raw_chapter_ids, list):
        return [
            value
            for raw in raw_chapter_ids
            if (value := _optional_uuid(raw)) is not None
        ]
    raw_numbers = (production.extra or {}).get(
        "pending_incremental_storyboard_episode_numbers"
    )
    source_step = None
    if not isinstance(raw_numbers, list):
        step_result = await db.execute(
            select(AgentStep)
            .where(
                AgentStep.production_id == production.id,
                AgentStep.stage == "source_analysis",
                AgentStep.scope_type == "production",
            )
            .order_by(AgentStep.input_version.desc(), AgentStep.created_at.desc())
            .limit(1)
        )
        source_step = step_result.scalar_one_or_none()
        if source_step is None or not (source_step.extra or {}).get("incremental"):
            return None
    episode_numbers = (
        {int(number) for number in raw_numbers}
        if isinstance(raw_numbers, list)
        else set()
    )
    result = await db.execute(
        select(ProjectChapter)
        .where(
            ProjectChapter.project_id == production.project_id,
            ProjectChapter.user_id == production.user_id,
            ProjectChapter.is_enabled.is_(True),
            ProjectChapter.extra["agent_production_id"].as_string()
            == str(production.id),
        )
        .order_by(ProjectChapter.sort_order, ProjectChapter.id)
    )
    chapters = list(result.scalars().all())
    if source_step is not None:
        return [
            chapter.id
            for chapter in chapters
            if str((chapter.extra or {}).get("agent_step_id") or "")
            == str(source_step.id)
        ]
    return [
        chapter.id
        for chapter in chapters
        if int((chapter.extra or {}).get("episode_number") or 0)
        in episode_numbers
    ]


async def _get_context(
    db: AsyncSession,
    production_id: UUID,
    user_id: UUID,
    *,
    lock: bool,
) -> CoreAssetContext:
    production_query = (
        select(AgentProduction)
        .join(Project, Project.id == AgentProduction.project_id)
        .where(
            AgentProduction.id == production_id,
            AgentProduction.user_id == user_id,
            Project.user_id == user_id,
            Project.is_enabled.is_(True),
        )
    )
    if lock:
        production_query = production_query.with_for_update(of=AgentProduction)
    production_result = await db.execute(production_query)
    production = production_result.scalar_one_or_none()
    if production is None:
        raise AppException("整剧任务不存在", code=40430, status_code=404)
    lock_result = await db.execute(
        select(AgentCoreAssetLock)
        .where(
            AgentCoreAssetLock.production_id == production.id,
            AgentCoreAssetLock.status == "active",
        )
        .order_by(AgentCoreAssetLock.version.desc())
        .limit(1)
    )
    active_lock = lock_result.scalar_one_or_none()
    bible_result = await db.execute(
        select(SeriesBibleVersion)
        .where(
            SeriesBibleVersion.production_id == production.id,
            SeriesBibleVersion.status == "confirmed",
        )
        .order_by(SeriesBibleVersion.version.desc())
        .limit(1)
    )
    bible = bible_result.scalar_one_or_none()
    if bible is None and active_lock is not None:
        bible = await db.get(SeriesBibleVersion, active_lock.bible_version_id)
    if bible is None:
        raise AppException("故事圣经尚未确认", code=40932, status_code=409)
    candidate_result = await db.execute(
        select(AgentAssetCandidate).where(
            AgentAssetCandidate.bible_version_id == bible.id,
            AgentAssetCandidate.review_status == "materialized",
            AgentAssetCandidate.materialized_asset_id.is_not(None),
        )
    )
    candidates = list(candidate_result.scalars().all())
    entries = await _load_entries(db, production, candidates, lock_assets=lock)
    return CoreAssetContext(production, bible, entries, active_lock)


def _require_core_asset_editable(context: CoreAssetContext) -> None:
    if (
        context.production.current_stage != "core_assets"
        or context.production.status in {"completed", "cancelled"}
        or context.active_lock is not None
    ):
        raise AppException("当前整剧阶段不允许修改核心资产", code=40932, status_code=409)


def _require_core_asset_image_editable(context: CoreAssetContext) -> None:
    if context.production.status not in {
        "planning",
        "running",
        "waiting_approval",
        "completed",
    }:
        raise AppException("当前整剧状态不允许修改核心资产图片", code=40932, status_code=409)


def _managed_entry(
    context: CoreAssetContext,
    asset_type: str,
    asset_id: UUID,
) -> CoreAssetEntry:
    entry = context.entries.get((asset_type, asset_id))
    if entry is None or entry.asset is None:
        raise AppException("核心资产不存在或已删除", code=40434, status_code=404)
    return entry


async def _managed_variant(
    db: AsyncSession,
    entry: CoreAssetEntry,
    asset_type: str,
    variant_id: UUID,
    *,
    lock: bool,
) -> AgentAssetVariant:
    query = select(AgentAssetVariant).where(
        AgentAssetVariant.id == variant_id,
        AgentAssetVariant.base_candidate_id == entry.candidate.id,
        AgentAssetVariant.asset_type == asset_type,
        AgentAssetVariant.review_status != "rejected",
    )
    if lock:
        query = query.with_for_update(of=AgentAssetVariant)
    result = await db.execute(query)
    variant = result.scalar_one_or_none()
    if variant is None:
        raise AppException("资产变体不存在", code=40440, status_code=404)
    return variant


async def _variants_by_candidate_ids(
    db: AsyncSession,
    candidate_ids: List[UUID],
) -> Dict[UUID, List[AgentAssetVariant]]:
    if not candidate_ids:
        return {}
    result = await db.execute(
        select(AgentAssetVariant)
        .where(
            AgentAssetVariant.base_candidate_id.in_(candidate_ids),
            AgentAssetVariant.review_status != "rejected",
        )
        .order_by(AgentAssetVariant.created_at, AgentAssetVariant.id)
    )
    grouped: Dict[UUID, List[AgentAssetVariant]] = {}
    for variant in result.scalars().all():
        grouped.setdefault(variant.base_candidate_id, []).append(variant)
    return grouped


def _managed_asset_payload(
    entry: CoreAssetEntry,
    variants: List[AgentAssetVariant],
) -> Dict[str, Any]:
    asset = entry.asset
    candidate = entry.candidate
    return {
        "asset_type": candidate.asset_type,
        "asset_id": asset.id,
        "candidate_id": candidate.id,
        "canonical_name": candidate.canonical_name,
        "aliases": list(candidate.aliases or []),
        "content": dict(candidate.content or {}),
        "reference_image": asset.reference_image,
        "image_generation_status": (asset.extra or {}).get("image_generation_status"),
        "lock_version": candidate.lock_version,
        "variants": variants,
        "created_at": candidate.created_at,
        "updated_at": candidate.updated_at,
    }


def _entry_matches_keyword(entry: CoreAssetEntry, keyword: str) -> bool:
    candidate = entry.candidate
    searchable = [
        candidate.canonical_name,
        *(candidate.aliases or []),
        json.dumps(candidate.content or {}, ensure_ascii=False, default=str),
    ]
    return any(keyword in str(value or "").lower() for value in searchable)


def _new_managed_asset(
    production: AgentProduction,
    payload: CoreAssetCreateRequest,
) -> Any:
    content = payload.content or {}
    common = {
        "project_id": production.project_id,
        "user_id": production.user_id,
        "name": payload.canonical_name,
        "description": _managed_text(content, "description"),
        "prompt": _managed_text(content, "prompt"),
        "source_content": _managed_text(content, "source_content"),
        "extra": {},
        "is_enabled": True,
    }
    if payload.asset_type == "character":
        return ProjectCharacter(
            **common,
            aliases=payload.aliases,
            identity=_managed_text(content, "identity", max_length=128),
            gender=_managed_text(content, "gender", max_length=32),
            age=_managed_text(content, "age", max_length=64),
            appearance=_managed_text(content, "appearance"),
            personality=_managed_text(content, "personality"),
            relationship=_managed_text(content, "relationship", fallback="relationships"),
            costume=_managed_text(content, "costume"),
        )
    common["extra"] = {"aliases": payload.aliases}
    if payload.asset_type == "scene":
        return ProjectScene(
            **common,
            location=_managed_text(content, "location", max_length=255),
            time_of_day=_managed_text(content, "time_of_day", fallback="time", max_length=64),
            environment=_managed_text(content, "environment"),
            atmosphere=_managed_text(content, "atmosphere"),
        )
    return ProjectProp(
        **common,
        category=_managed_text(content, "category", fallback="prop_type", max_length=64),
        appearance=_managed_text(content, "appearance"),
        function=_managed_text(content, "function"),
    )


def _sync_managed_asset(asset: Any, candidate: AgentAssetCandidate) -> None:
    asset.name = candidate.canonical_name
    content = candidate.content or {}
    common_fields = {
        "description": ("description", None, None),
        "prompt": ("prompt", None, None),
        "source_content": ("source_content", None, None),
    }
    typed_fields: Dict[str, Tuple[str, Optional[str], Optional[int]]] = {}
    if isinstance(asset, ProjectCharacter):
        asset.aliases = list(candidate.aliases or [])
        typed_fields = {
            "identity": ("identity", None, 128),
            "gender": ("gender", None, 32),
            "age": ("age", None, 64),
            "appearance": ("appearance", None, None),
            "personality": ("personality", None, None),
            "relationship": ("relationship", "relationships", None),
            "costume": ("costume", None, None),
        }
    else:
        asset.extra = {**(asset.extra or {}), "aliases": list(candidate.aliases or [])}
        if isinstance(asset, ProjectScene):
            typed_fields = {
                "location": ("location", None, 255),
                "time_of_day": ("time_of_day", "time", 64),
                "environment": ("environment", None, None),
                "atmosphere": ("atmosphere", None, None),
            }
        else:
            typed_fields = {
                "category": ("category", "prop_type", 64),
                "appearance": ("appearance", None, None),
                "function": ("function", None, None),
            }
    for attribute, (key, fallback, max_length) in {**common_fields, **typed_fields}.items():
        if key in content or (fallback is not None and fallback in content):
            setattr(
                asset,
                attribute,
                _managed_text(content, key, fallback=fallback, max_length=max_length),
            )


def _managed_text(
    content: Dict[str, Any],
    key: str,
    *,
    fallback: Optional[str] = None,
    max_length: Optional[int] = None,
) -> Optional[str]:
    value = content.get(key)
    if value is None and fallback is not None:
        value = content.get(fallback)
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        text_value = json.dumps(value, ensure_ascii=False)
    else:
        text_value = str(value).strip()
    if not text_value:
        return None
    return text_value[:max_length] if max_length else text_value


def _normalize_name(value: Any) -> str:
    return re.sub(r"[\W_]+", "", str(value or "").strip().lower(), flags=re.UNICODE)


async def _load_entries(
    db: AsyncSession,
    production: AgentProduction,
    candidates: List[AgentAssetCandidate],
    *,
    lock_assets: bool,
) -> Dict[Tuple[str, UUID], CoreAssetEntry]:
    entries: Dict[Tuple[str, UUID], CoreAssetEntry] = {}
    for asset_type, model in ASSET_MODELS.items():
        typed_candidates = [
            candidate
            for candidate in candidates
            if candidate.asset_type == asset_type and candidate.materialized_asset_id is not None
        ]
        ids = [candidate.materialized_asset_id for candidate in typed_candidates]
        assets: Dict[UUID, Any] = {}
        if ids:
            query = select(model).where(
                model.id.in_(ids),
                model.project_id == production.project_id,
                model.user_id == production.user_id,
                model.is_enabled.is_(True),
            )
            if lock_assets:
                query = query.with_for_update(of=model)
            result = await db.execute(query)
            assets = {asset.id: asset for asset in result.scalars().all()}
        for candidate in typed_candidates:
            asset_id = candidate.materialized_asset_id
            if asset_id is not None:
                entries[(asset_type, asset_id)] = CoreAssetEntry(
                    candidate,
                    assets.get(asset_id),
                )
    return entries


def _requested_entries(
    context: CoreAssetContext, items: List[Any]
) -> List[Tuple[Any, CoreAssetEntry]]:
    result = []
    for item in items:
        entry = context.entries.get((item.asset_type, item.asset_id))
        if entry is None:
            raise AppException("核心资产不属于当前故事圣经", code=40434, status_code=404)
        result.append((item, entry))
    return result


def _selection_snapshot(
    context: CoreAssetContext,
    assets: List[Any],
    *,
    require_character_scene: bool = True,
) -> List[Dict[str, Any]]:
    entries = _requested_entries(context, assets)
    selected_types = {item.asset_type for item, _entry in entries}
    if require_character_scene and not {"character", "scene"}.issubset(selected_types):
        raise AppException("核心资产至少需要选择一个角色和一个场景", code=40058, status_code=400)
    snapshot = []
    for item, entry in entries:
        asset = entry.asset
        if asset is None:
            raise AppException("核心资产不存在或已停用", code=40434, status_code=404)
        snapshot.append(
            {
                "asset_type": item.asset_type,
                "asset_id": str(item.asset_id),
                "candidate_id": str(entry.candidate.id),
                "name": asset.name,
                "reference_image": asset.reference_image,
            }
        )
    return sorted(snapshot, key=lambda item: (item["asset_type"], item["asset_id"]))


def _all_asset_snapshot(context: CoreAssetContext) -> List[Dict[str, Any]]:
    refs = [
        CoreAssetRef(asset_type=asset_type, asset_id=asset_id)
        for (asset_type, asset_id), entry in context.entries.items()
        if entry.asset is not None
    ]
    return _selection_snapshot(context, refs, require_character_scene=False)


async def _attach_variant_snapshots(
    db: AsyncSession,
    context: CoreAssetContext,
    snapshots: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    candidate_ids = {
        candidate_id
        for item in snapshots
        if (candidate_id := _optional_uuid(item.get("candidate_id"))) is not None
    }
    if not candidate_ids:
        return snapshots
    result = await db.execute(
        select(AgentAssetVariant)
        .where(
            AgentAssetVariant.base_candidate_id.in_(candidate_ids),
            AgentAssetVariant.production_id == context.production.id,
            AgentAssetVariant.bible_version_id == context.bible.id,
            AgentAssetVariant.review_status == "ready",
        )
        .order_by(AgentAssetVariant.created_at, AgentAssetVariant.id)
    )
    variants_by_candidate: Dict[UUID, List[Dict[str, Any]]] = {}
    for variant in result.scalars().all():
        variants_by_candidate.setdefault(variant.base_candidate_id, []).append(
            {
                "variant_id": str(variant.id),
                "name": variant.canonical_name,
                "variant_type": variant.variant_type,
                "reference_image": variant.reference_image,
            }
        )
    return [
        {
            **item,
            "variants": variants_by_candidate.get(
                _optional_uuid(item.get("candidate_id")),
                [],
            ),
        }
        for item in snapshots
    ]


async def _build_impact(
    db: AsyncSession,
    context: CoreAssetContext,
    proposed: List[Dict[str, Any]],
) -> Dict[str, Any]:
    current = list((context.active_lock.assets if context.active_lock else []) or [])
    current_map = _snapshot_map(current)
    proposed_map = _snapshot_map(proposed)
    changes = []
    for key in sorted(
        set(current_map) | set(proposed_map), key=lambda item: (item[0], str(item[1]))
    ):
        before = current_map.get(key)
        after = proposed_map.get(key)
        if before is None:
            change_type = "added"
        elif after is None:
            change_type = "removed"
        elif (
            before.get("reference_image") != after.get("reference_image")
            or before.get("variants") != after.get("variants")
        ):
            change_type = "reference_changed"
        else:
            continue
        source = after or before or {}
        changes.append(
            {
                "asset_type": key[0],
                "asset_id": str(key[1]),
                "name": source.get("name") or "",
                "change_type": change_type,
                "previous_reference_image": before.get("reference_image") if before else None,
                "proposed_reference_image": after.get("reference_image") if after else None,
            }
        )
    changed_keys = (
        {(item["asset_type"], UUID(item["asset_id"])) for item in changes}
        if context.active_lock is not None
        else set()
    )
    storyboard_result = await db.execute(
        select(ProjectStoryboard).where(
            ProjectStoryboard.project_id == context.production.project_id,
            ProjectStoryboard.user_id == context.production.user_id,
            ProjectStoryboard.is_enabled.is_(True),
        )
    )
    storyboards = list(storyboard_result.scalars().all())
    affected_ids = sorted(
        [
            storyboard.id
            for storyboard in storyboards
            if _storyboard_asset_keys(storyboard) & changed_keys
        ],
        key=str,
    )
    image_count, video_count = await _generated_media_counts(db, affected_ids)
    warnings = []
    if context.active_lock is None:
        warnings.append("首次锁定不会使现有下游内容失效。")
    elif not changes:
        warnings.append("核心资产选择和参考图均未变化。")
    unbound_count = sum(
        bool((storyboard.extra or {}).get("image_generation_result"))
        and not _storyboard_asset_keys(storyboard)
        for storyboard in storyboards
    )
    if unbound_count:
        warnings.append(f"有 {unbound_count} 个历史分镜未保存稳定资产 ID，未纳入自动影响范围。")
    fingerprint_payload = {
        "current_lock_version": context.active_lock.version if context.active_lock else 0,
        "proposed": proposed,
        "changes": changes,
        "affected_storyboard_ids": [str(value) for value in affected_ids],
        "affected_image_count": image_count,
        "affected_video_count": video_count,
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {
        "current_lock_version": context.active_lock.version if context.active_lock else 0,
        "proposed_lock_version": (context.active_lock.version if context.active_lock else 0) + 1,
        "impact_fingerprint": fingerprint,
        "changes": changes,
        "affected_storyboard_ids": [str(value) for value in affected_ids],
        "affected_storyboard_count": len(affected_ids),
        "affected_image_count": image_count,
        "affected_video_count": video_count,
        "warnings": warnings,
    }


async def _generated_media_counts(db: AsyncSession, storyboard_ids: List[UUID]) -> Tuple[int, int]:
    if not storyboard_ids:
        return 0, 0
    result = await db.execute(
        select(ProjectGeneratedAsset.media_type, func.count())
        .where(
            ProjectGeneratedAsset.target_type == "storyboard",
            ProjectGeneratedAsset.target_id.in_(storyboard_ids),
            ProjectGeneratedAsset.media_type.in_(("image", "video")),
            ProjectGeneratedAsset.is_enabled.is_(True),
        )
        .group_by(ProjectGeneratedAsset.media_type)
    )
    counts = {str(media_type): int(count) for media_type, count in result.all()}
    return counts.get("image", 0), counts.get("video", 0)


async def _invalidate_affected_outputs(
    db: AsyncSession,
    storyboard_ids: List[Any],
    lock_id: UUID,
    version: int,
) -> None:
    parsed_ids = [_optional_uuid(value) for value in storyboard_ids]
    ids = [value for value in parsed_ids if value is not None]
    if not ids:
        return
    storyboard_result = await db.execute(
        select(ProjectStoryboard).where(ProjectStoryboard.id.in_(ids)).with_for_update()
    )
    for storyboard in storyboard_result.scalars().all():
        extra = dict(storyboard.extra or {})
        if extra.get("image_generation_status") in {"success", "selected"}:
            extra["image_generation_status"] = "invalidated"
        if extra.get("video_generation_status") in {"success", "selected"}:
            extra["video_generation_status"] = "invalidated"
        extra["invalidated_by_core_asset_lock_id"] = str(lock_id)
        extra["invalidated_by_core_asset_lock_version"] = version
        storyboard.extra = extra
    history_result = await db.execute(
        select(ProjectGeneratedAsset).where(
            ProjectGeneratedAsset.target_type == "storyboard",
            ProjectGeneratedAsset.target_id.in_(ids),
            ProjectGeneratedAsset.media_type.in_(("image", "video")),
            ProjectGeneratedAsset.is_enabled.is_(True),
        )
    )
    for history in history_result.scalars().all():
        history.extra = {
            **(history.extra or {}),
            "validity_status": "invalidated",
            "invalidated_by_core_asset_lock_id": str(lock_id),
            "invalidated_by_core_asset_lock_version": version,
        }


def _snapshot_map(values: List[Dict[str, Any]]) -> Dict[Tuple[str, UUID], Dict[str, Any]]:
    result = {}
    for item in values:
        asset_id = _optional_uuid(item.get("asset_id"))
        asset_type = str(item.get("asset_type") or "")
        if asset_id is not None and asset_type in ASSET_MODELS:
            result[(asset_type, asset_id)] = item
    return result


def _storyboard_asset_keys(storyboard: ProjectStoryboard) -> set[Tuple[str, UUID]]:
    extra = storyboard.extra or {}
    result: set[Tuple[str, UUID]] = set()
    for field in (
        "agent_asset_ids",
        "image_reference_asset_ids",
        "video_reference_asset_ids",
    ):
        mapping = extra.get(field)
        if not isinstance(mapping, dict):
            continue
        for asset_type in ASSET_MODELS:
            for value in mapping.get(asset_type) or []:
                asset_id = _optional_uuid(value)
                if asset_id is not None:
                    result.add((asset_type, asset_id))
    return result


def _has_active_image_task(asset: Optional[Any]) -> bool:
    return bool(
        asset is not None
        and (asset.extra or {}).get("image_generation_status") in {"pending", "running"}
        and (asset.extra or {}).get("image_generation_task_record_id")
    )


def _has_active_variant_image_task(variant: AgentAssetVariant) -> bool:
    return bool(
        (variant.extra or {}).get("image_generation_status") in {"pending", "running"}
        and (variant.extra or {}).get("image_generation_task_record_id")
    )


def _generation_error(asset_type: str, asset_id: UUID, code: int, message: str) -> Dict[str, Any]:
    return {
        "asset_type": asset_type,
        "asset_id": asset_id,
        "submitted": False,
        "points_cost": 0,
        "error_code": code,
        "error_message": message,
    }


def _lock_result(lock_record: AgentCoreAssetLock, *, already_locked: bool) -> Dict[str, Any]:
    return {
        "lock_id": lock_record.id,
        "version": lock_record.version,
        "status": lock_record.status,
        "assets": lock_record.assets or [],
        "impact": lock_record.impact or {},
        "already_locked": already_locked,
    }


def _optional_uuid(value: Any) -> Optional[UUID]:
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None
