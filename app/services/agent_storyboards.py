from copy import deepcopy
from collections import defaultdict
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException
from app.core.timezone import beijing_datetime
from app.models.agent_core_asset import AgentCoreAssetLock
from app.models.agent_production import AgentEvent, AgentProduction, AgentStep
from app.models.agent_review import AgentEpisodeReview
from app.models.agent_story_bible import AgentAssetVariant
from app.models.ai_model import AiModel
from app.models.project_chapter import ProjectChapter
from app.models.project_generated_asset import ProjectGeneratedAsset
from app.models.project import Project
from app.models.project_storyboard import ProjectStoryboard
from app.models.style import Style
from app.models.user import User
from app.schemas.agent_batch_production import AgentBatchDispatchRequest
from app.schemas.agent_storyboard import (
    AgentStoryboardAssetBinding,
    AgentStoryboardCopyRequest,
    AgentStoryboardCreateRequest,
    AgentStoryboardGenerateRequest,
    AgentStoryboardReorderRequest,
    AgentStoryboardUpdateRequest,
)
from app.schemas.agent_storyboard_media import AgentStoryboardVideoConfigRequest
from app.services.agent_batch_productions import (
    dispatch_batch_production,
    get_batch_production,
)
from app.services.agent_storyboard_bindings import (
    AGENT_ASSET_BINDING_VERSION,
    storyboard_estimated_duration_seconds,
)
from app.services.agent_storyboard_episode_state import (
    build_storyboard_episode_progress,
    effective_storyboard_episode_status,
    require_visible_storyboard_episode,
)
from app.services.agent_storyboard_prompts import (
    asset_token_keys,
    render_asset_tokens,
    render_storyboard_shots,
    tokenize_asset_text,
    tokenize_storyboard_shots,
)
from app.services.agent_video_settings import require_agent_video_defaults
from app.services.project_storyboards import (
    build_agent_storyboard_prompt,
    estimate_agent_shot_group_duration,
)


ACTIVE_STATUSES = {"pending", "running"}
SUCCESS_STATUSES = {"success", "selected"}


async def get_agent_storyboards(
    db: AsyncSession,
    production_id: UUID,
    user_id: UUID,
) -> Dict[str, Any]:
    batch = await get_batch_production(db, production_id, user_id)
    production = await _get_owned_production(db, production_id, user_id)
    chapters = await _production_chapters(db, production)
    storyboards = await _production_storyboards(db, production, chapters)
    by_chapter: Dict[UUID, List[ProjectStoryboard]] = defaultdict(list)
    for storyboard in storyboards:
        by_chapter[storyboard.chapter_id].append(storyboard)
    progress = build_storyboard_episode_progress(
        chapters,
        {chapter_id: len(items) for chapter_id, items in by_chapter.items()},
    )
    visible_chapter_ids = set(progress.visible_chapter_ids)
    visible_storyboards = [
        storyboard
        for storyboard in storyboards
        if storyboard.chapter_id in visible_chapter_ids
    ]
    episode_analyses = [
        _episode_analysis_payload(
            chapter,
            effective_storyboard_episode_status(
                chapter,
                len(by_chapter.get(chapter.id, [])),
            ),
        )
        for chapter in chapters
    ]

    episodes = []
    total_duration = 0
    for chapter in chapters:
        if chapter.id not in visible_chapter_ids:
            continue
        items = by_chapter[chapter.id]
        status = str((chapter.extra or {}).get("storyboard_analysis_status") or "not_started")
        item_payloads = [_storyboard_payload(item) for item in items]
        episode_duration = sum(item["estimated_duration_seconds"] for item in item_payloads)
        total_duration += episode_duration
        episodes.append(
            {
                "chapter_id": chapter.id,
                "episode_number": int((chapter.extra or {}).get("episode_number") or 0),
                "title": chapter.title,
                "status": status,
                "revision": _episode_revision(chapter),
                "task_record_id": _optional_uuid(
                    (chapter.extra or {}).get("storyboard_analysis_task_record_id")
                ),
                "estimated_duration_seconds": episode_duration,
                "storyboards": item_payloads,
            }
        )
    chapter_by_id = {chapter.id: chapter for chapter in chapters}
    current_chapter = (
        chapter_by_id.get(progress.current_chapter_id)
        if progress.current_chapter_id is not None
        else None
    )
    failed_chapter_ids = set(progress.failed_chapter_ids)
    failed_episodes = [
        item for item in episode_analyses if item["chapter_id"] in failed_chapter_ids
    ]
    return {
        "production_id": production.id,
        "core_asset_lock_version": batch["core_asset_lock_version"],
        "phase": batch["phase"],
        "status": batch["status"],
        "current_stage": batch["current_stage"],
        "episode_count": progress.total_episode_count,
        "completed_episode_count": progress.completed_episode_count,
        "remaining_episode_count": progress.remaining_episode_count,
        "analysis_complete": progress.analysis_complete,
        "episode_analyses": episode_analyses,
        "current_analysis": (
            _episode_analysis_payload(current_chapter, progress.current_status)
            if current_chapter is not None
            else None
        ),
        "failed_episodes": failed_episodes,
        "storyboard_count": len(visible_storyboards),
        "estimated_duration_seconds": total_duration,
        "active_task_count": batch["active_task_count"],
        "failed_item_count": batch["failed_item_count"],
        "can_generate": batch["phase"] in {"not_started", "storyboards"}
        and batch["status"] not in {"paused", "cancelled", "completed"},
        "episodes": episodes,
    }


async def get_agent_storyboard_asset_options(
    db: AsyncSession,
    production_id: UUID,
    chapter_id: UUID,
    storyboard_id: UUID,
    user_id: UUID,
) -> Dict[str, Any]:
    production = await _get_owned_production(db, production_id, user_id)
    core_lock = await _active_core_lock(db, production.id)
    if core_lock is None:
        raise AppException("核心资产尚未锁定", code=40950, status_code=409)
    storyboard, chapter = await _locked_storyboard(db, production, storyboard_id)
    if chapter.id != chapter_id:
        raise AppException("Agent 分镜不存在", code=40410, status_code=404)
    await _require_storyboard_episode_ready(db, production, chapter.id)
    bindings = _storyboard_asset_bindings(storyboard)
    bound_by_asset = {
        (str(item["asset_type"]), item["asset_id"]): item for item in bindings
    }
    candidate_ids = [
        value
        for value in (_optional_uuid(item.get("candidate_id")) for item in core_lock.assets or [])
        if value is not None
    ]
    variants: Dict[UUID, List[AgentAssetVariant]] = defaultdict(list)
    if candidate_ids:
        result = await db.execute(
            select(AgentAssetVariant).where(
                AgentAssetVariant.production_id == production.id,
                AgentAssetVariant.bible_version_id == core_lock.bible_version_id,
                AgentAssetVariant.base_candidate_id.in_(candidate_ids),
                AgentAssetVariant.review_status == "ready",
            )
        )
        for variant in result.scalars().all():
            variants[variant.base_candidate_id].append(variant)
    episode_number = int((chapter.extra or {}).get("episode_number") or 0)
    items = []
    for snapshot in core_lock.assets or []:
        asset_type = str(snapshot.get("asset_type") or "")
        asset_id = _optional_uuid(snapshot.get("asset_id"))
        candidate_id = _optional_uuid(snapshot.get("candidate_id"))
        if (
            asset_type not in {"character", "scene", "prop"}
            or asset_id is None
            or candidate_id is None
        ):
            continue
        binding = bound_by_asset.get((asset_type, asset_id))
        selected_variant_id = binding.get("variant_id") if binding else None
        applicable_variants = [
            variant
            for variant in variants.get(candidate_id, [])
            if not variant.episode_numbers or episode_number in set(variant.episode_numbers)
        ]
        selected_variant = next(
            (
                variant
                for variant in applicable_variants
                if variant.id == selected_variant_id
            ),
            None,
        )
        canonical_name = str(
            snapshot.get("name")
            or snapshot.get("canonical_name")
            or asset_id
        )
        mention_label = (
            selected_variant.canonical_name
            if selected_variant is not None
            else canonical_name
        )
        mention_reference_image = (
            selected_variant.reference_image
            if selected_variant is not None
            else snapshot.get("reference_image")
        )
        items.append(
            {
                "asset_type": asset_type,
                "asset_id": asset_id,
                "candidate_id": candidate_id,
                "canonical_name": canonical_name,
                "reference_image": snapshot.get("reference_image"),
                "bound": binding is not None,
                "binding_key": binding.get("binding_key") if binding else None,
                "selected_variant_id": selected_variant_id,
                "mention_text": f"@{mention_label}" if binding is not None else None,
                "mention_reference_image": (
                    mention_reference_image if binding is not None else None
                ),
                "mention_enabled": bool(binding is not None and mention_reference_image),
                "variants": [
                    {
                        "variant_id": variant.id,
                        "canonical_name": variant.canonical_name,
                        "variant_type": variant.variant_type,
                        "description": variant.description,
                        "trigger_reason": variant.trigger_reason,
                        "reference_image": variant.reference_image,
                        "selected": variant.id == selected_variant_id,
                    }
                    for variant in sorted(
                        applicable_variants,
                        key=lambda item: (item.canonical_name, item.id),
                    )
                ],
            }
        )
    return {
        "production_id": production.id,
        "chapter_id": chapter.id,
        "storyboard_id": storyboard.id,
        "episode_number": episode_number,
        "core_asset_lock_version": core_lock.version,
        "storyboard_revision": _storyboard_revision(storyboard),
        "items": sorted(
            items,
            key=lambda item: (item["asset_type"], item["canonical_name"]),
        ),
    }


async def update_agent_storyboard_video_config(
    db: AsyncSession,
    production_id: UUID,
    chapter_id: UUID,
    storyboard_id: UUID,
    user: User,
    payload: AgentStoryboardVideoConfigRequest,
) -> Dict[str, Any]:
    production = await _get_owned_production(db, production_id, user.id)
    core_lock = await _require_core_lock_version(
        db,
        production.id,
        payload.expected_core_asset_lock_version,
    )
    storyboard, chapter = await _locked_storyboard(db, production, storyboard_id)
    if chapter.id != chapter_id:
        raise AppException("Agent 分镜不存在", code=40410, status_code=404)
    await _require_storyboard_episode_ready(db, production, chapter.id)
    _check_storyboard_revision(storyboard, payload.expected_storyboard_revision)
    _assert_storyboard_idle(storyboard)
    require_agent_video_defaults(
        production,
        payload.video_model_id,
        payload.video_resolution,
    )
    model = (
        await db.execute(
            select(AiModel).where(
                AiModel.id == payload.video_model_id,
                AiModel.model_type == "video",
                AiModel.is_enabled.is_(True),
            )
        )
    ).scalar_one_or_none()
    if model is None:
        raise AppException("视频模型不存在或已禁用", code=40404, status_code=404)
    extra = dict(storyboard.extra or {})
    current_config = dict(extra.get("agent_video_config") or {})
    current_version = int(current_config.get("version") or 0)
    if payload.expected_config_version != current_version:
        raise AppException(
            f"分镜视频配置版本冲突，当前版本为 {current_version}",
            code=40992,
            status_code=409,
            data={
                "expected_config_version": payload.expected_config_version,
                "current_config_version": current_version,
            },
        )
    next_config = {
        "version": current_version + 1,
        "video_model_id": str(model.id),
        "video_resolution": payload.video_resolution,
        "estimated_duration_seconds": payload.estimated_duration_seconds,
    }
    comparable = {key: value for key, value in next_config.items() if key != "version"}
    current_comparable = {
        key: current_config.get(key) for key in comparable
    }
    if comparable == current_comparable:
        next_config["version"] = max(1, current_version)
    duration_changed = (
        storyboard_estimated_duration_seconds(storyboard)
        != payload.estimated_duration_seconds
    )
    extra["agent_video_config"] = next_config
    if duration_changed:
        shots = list(extra.get("agent_shots") or [])
        notes = str(extra.get("agent_storyboard_prompt_notes") or "")
        template = await _build_prompt_template(
            db,
            production,
            shots,
            notes,
            payload.estimated_duration_seconds,
        )
        labels = _binding_labels(extra)
        effective_prompt = render_asset_tokens(template, labels)
        extra.update(
            {
                "agent_storyboard_prompt_template": template,
                "agent_storyboard_prompt": effective_prompt,
                "estimated_duration_seconds": payload.estimated_duration_seconds,
                "agent_duration_source": "user",
                "agent_storyboard_revision": _storyboard_revision(storyboard) + 1,
            }
        )
        storyboard.video_prompt = effective_prompt
        storyboard.duration_suggestion = f"{payload.estimated_duration_seconds}秒"
        storyboard.extra = extra
        await _invalidate_storyboard_media(db, storyboard, {"video"})
        extra = dict(storyboard.extra or {})
        await _invalidate_episode_review(db, production.id, chapter.id)
        await _reopen_batch_for_storyboard_edit(db, production, "videos")
        _bump_episode_revision(chapter)
        production.lock_version += 1
    storyboard.extra = extra
    storyboard.updated_at = beijing_datetime()
    if comparable != current_comparable or duration_changed:
        db.add(
            AgentEvent(
                production_id=production.id,
                actor_user_id=user.id,
                event_type="storyboard.video_config_updated",
                source="user",
                payload={
                    "storyboard_id": str(storyboard.id),
                    "chapter_id": str(chapter.id),
                    "config_version": next_config["version"],
                    "video_model_id": str(model.id),
                    "video_resolution": payload.video_resolution,
                    "estimated_duration_seconds": payload.estimated_duration_seconds,
                    "core_asset_lock_version": core_lock.version,
                },
            )
        )
    await db.commit()
    return {
        "storyboard_id": storyboard.id,
        "chapter_id": chapter.id,
        "storyboard_revision": _storyboard_revision(storyboard),
        "episode_revision": _episode_revision(chapter),
        "config_version": int(next_config["version"]),
        "video_model_id": model.id,
        "video_resolution": payload.video_resolution,
        "estimated_duration_seconds": payload.estimated_duration_seconds,
    }


async def generate_agent_storyboards(
    db: AsyncSession,
    production_id: UUID,
    user: User,
    payload: AgentStoryboardGenerateRequest,
    *,
    chapter_ids: Optional[List[UUID]] = None,
) -> Dict[str, Any]:
    status = await get_batch_production(db, production_id, user.id)
    if status["core_asset_lock_version"] != payload.expected_core_asset_lock_version:
        raise AppException(
            f"核心资产锁版本冲突，当前版本为 {status['core_asset_lock_version']}",
            code=40955,
            status_code=409,
            data={
                "expected_core_asset_lock_version": payload.expected_core_asset_lock_version,
                "current_core_asset_lock_version": status["core_asset_lock_version"],
            },
        )
    if chapter_ids is not None or status["phase"] in {
        "not_started",
        "storyboards",
        "episode_videos",
    }:
        await dispatch_batch_production(
            db,
            production_id,
            user,
            AgentBatchDispatchRequest(
                expected_core_asset_lock_version=payload.expected_core_asset_lock_version,
                idempotency_key=payload.idempotency_key,
                max_tasks=50,
            ),
            scope_ids=chapter_ids,
            replace_storyboard_scope=True,
        )
    return await get_agent_storyboards(db, production_id, user.id)


def _episode_analysis_payload(
    chapter: ProjectChapter,
    status: Optional[str],
) -> Dict[str, Any]:
    extra = chapter.extra or {}
    error = str(
        extra.get("storyboard_analysis_failed_reason")
        or extra.get("storyboard_analysis_status_error")
        or extra.get("storyboard_analysis_error")
        or ""
    ) or None
    return {
        "chapter_id": chapter.id,
        "episode_number": int(extra.get("episode_number") or 0),
        "title": chapter.title,
        "status": status or "not_started",
        "task_record_id": _optional_uuid(extra.get("storyboard_analysis_task_record_id")),
        "error": error,
    }


async def update_agent_storyboard(
    db: AsyncSession,
    production_id: UUID,
    storyboard_id: UUID,
    user: User,
    payload: AgentStoryboardUpdateRequest,
) -> Dict[str, Any]:
    production = await _get_owned_production(db, production_id, user.id)
    if production.status in {"completed", "cancelled"}:
        raise AppException("当前整剧状态不允许修改分镜", code=40956, status_code=409)
    core_lock = await _require_core_lock_version(
        db,
        production.id,
        payload.expected_core_asset_lock_version,
    )
    storyboard, chapter = await _locked_storyboard(
        db,
        production,
        storyboard_id,
    )
    await _require_storyboard_episode_ready(db, production, chapter.id)
    extra = dict(storyboard.extra or {})
    revision = _storyboard_revision(storyboard)
    if payload.expected_revision is not None and payload.expected_revision != revision:
        raise AppException(
            f"分镜组版本冲突，当前版本为 {revision}",
            code=40959,
            status_code=409,
            data={
                "expected_revision": payload.expected_revision,
                "current_revision": revision,
            },
        )
    if any(
        str(extra.get(f"{media_type}_generation_status") or "") in ACTIVE_STATUSES
        for media_type in ("image", "video")
    ):
        raise AppException("分镜正在生成媒体，暂时不能修改", code=40956, status_code=409)

    current_bindings = _storyboard_asset_bindings(storyboard)
    current_labels = _binding_labels(extra)
    if current_bindings and not current_labels:
        normalized_bindings, normalized_context, current_labels = (
            await _validate_asset_bindings(
                db,
                production,
                core_lock,
                chapter,
                [AgentStoryboardAssetBinding.model_validate(item) for item in current_bindings],
                current_bindings,
            )
        )
        _store_binding_state(
            extra,
            core_lock,
            normalized_bindings,
            normalized_context,
            current_labels,
        )
    bindings = _serialize_bindings(current_bindings)
    variant_context = list(extra.get("agent_asset_variant_context") or [])
    labels = current_labels
    bindings_changed = False
    if payload.asset_bindings is not None:
        bindings, variant_context, labels = await _validate_asset_bindings(
            db,
            production,
            core_lock,
            chapter,
            payload.asset_bindings,
            current_bindings,
        )
        bindings_changed = bindings != _serialize_bindings(current_bindings)
        _store_binding_state(extra, core_lock, bindings, variant_context, labels)

    old_prompt = str(extra.get("agent_storyboard_prompt") or storyboard.video_prompt or "")
    prompt_template = str(extra.get("agent_storyboard_prompt_template") or "")
    if not prompt_template:
        prompt_template = tokenize_asset_text(old_prompt, _label_to_key(current_labels))

    current_shots = list(extra.get("agent_shots") or [])
    tokenized_shots = current_shots
    if payload.shots is not None:
        requested_shots = [
            {**item.model_dump(), "shot_number": index}
            for index, item in enumerate(payload.shots, start=1)
        ]
        shot_label_to_key = _label_to_key(current_labels)
        shot_label_to_key.update(_label_to_key(labels))
        tokenized_shots = tokenize_storyboard_shots(
            requested_shots,
            shot_label_to_key,
        )
    shots_changed = tokenized_shots != current_shots

    notes = (
        payload.prompt_notes.strip()
        if payload.prompt_notes is not None
        else str(extra.get("agent_storyboard_prompt_notes") or "")
    )
    notes_changed = notes != str(extra.get("agent_storyboard_prompt_notes") or "")
    resolved_shots = render_storyboard_shots(tokenized_shots, labels)
    calculated_duration = (
        estimate_agent_shot_group_duration(resolved_shots) if resolved_shots else 4
    )
    current_duration = storyboard_estimated_duration_seconds(storyboard)
    current_duration_source = str(extra.get("agent_duration_source") or "model")
    if payload.estimated_duration_seconds is not None:
        duration = payload.estimated_duration_seconds
        duration_source = "user"
    elif current_duration_source == "user" and not shots_changed:
        duration = current_duration
        duration_source = "user"
    else:
        duration = calculated_duration
        duration_source = "model"
    duration_changed = duration != current_duration
    if payload.storyboard_prompt is not None:
        prompt_template = tokenize_asset_text(
            payload.storyboard_prompt,
            _label_to_key(labels),
        )
    elif shots_changed or notes_changed or duration_changed:
        prompt_template = await _build_prompt_template(
            db,
            production,
            tokenized_shots,
            notes,
            duration,
        )

    invalid_binding_keys = (
        [key for key in asset_token_keys(prompt_template) if key not in labels]
        if payload.storyboard_prompt is not None
        else []
    )
    if invalid_binding_keys:
        raise AppException(
            "分镜提示词引用了未绑定的资产",
            code=40962,
            status_code=409,
            data={"invalid_binding_keys": invalid_binding_keys},
        )
    effective_prompt = render_asset_tokens(prompt_template, labels)
    prompt_changed = effective_prompt != old_prompt
    title_changed = payload.title is not None and payload.title != storyboard.title
    source_changed = (
        payload.source_content is not None
        and payload.source_content != storyboard.source_content
    )
    changed = any(
        (
            prompt_changed,
            bindings_changed,
            shots_changed,
            notes_changed,
            title_changed,
            source_changed,
            duration_changed,
        )
    )

    if payload.title is not None:
        storyboard.title = payload.title
    if payload.source_content is not None:
        storyboard.source_content = payload.source_content
    if shots_changed or bindings_changed:
        _apply_shot_summary(storyboard, resolved_shots)

    status, validation_errors = _storyboard_validation(
        tokenized_shots,
        duration,
        set(labels),
    )
    extra.update(
        {
            "agent_shots": tokenized_shots,
            "agent_storyboard_prompt_template": prompt_template,
            "agent_storyboard_prompt": effective_prompt,
            "agent_storyboard_prompt_notes": notes,
            "agent_storyboard_status": status,
            "agent_storyboard_validation_errors": validation_errors,
            "estimated_duration_seconds": duration,
            "agent_duration_source": duration_source,
        }
    )
    storyboard.duration_suggestion = f"{duration}秒"
    storyboard.video_prompt = effective_prompt
    storyboard.extra = extra
    if changed:
        extra["agent_storyboard_revision"] = revision + 1
        storyboard.extra = extra
        media_types = (
            {"image", "video"}
            if prompt_changed or bindings_changed or shots_changed or source_changed
            else {"video"}
        )
        await _invalidate_storyboard_media(db, storyboard, media_types)
        await _reopen_batch_for_storyboard_edit(
            db,
            production,
            "images" if "image" in media_types else "videos",
        )
        await _invalidate_episode_review(db, production.id, storyboard.chapter_id)
        _bump_episode_revision(chapter)
        production.lock_version += 1
        db.add(
            AgentEvent(
                production_id=production.id,
                actor_user_id=user.id,
                event_type="storyboard.updated",
                source="user",
                payload={
                    "storyboard_id": str(storyboard.id),
                    "chapter_id": str(storyboard.chapter_id),
                    "prompt_changed": prompt_changed,
                    "bindings_changed": bindings_changed,
                    "shots_changed": shots_changed,
                    "duration_changed": duration_changed,
                    "revision": revision + 1,
                },
            )
        )
    if changed:
        storyboard.updated_at = beijing_datetime()
    await db.commit()
    await db.refresh(storyboard)
    return _storyboard_payload(storyboard)


async def create_agent_storyboard(
    db: AsyncSession,
    production_id: UUID,
    chapter_id: UUID,
    user: User,
    payload: AgentStoryboardCreateRequest,
) -> Dict[str, Any]:
    production = await _get_owned_production(db, production_id, user.id)
    _assert_storyboard_mutation_allowed(production)
    await _require_core_lock_version(
        db,
        production.id,
        payload.expected_core_asset_lock_version,
    )
    chapter, storyboards = await _locked_episode_storyboards(
        db,
        production,
        chapter_id,
    )
    await _require_storyboard_episode_ready(db, production, chapter.id)
    _check_episode_revision(chapter, payload.expected_episode_revision)
    insert_index = len(storyboards)
    if payload.insert_after_storyboard_id is not None:
        insert_index = next(
            (
                index + 1
                for index, item in enumerate(storyboards)
                if item.id == payload.insert_after_storyboard_id
            ),
            -1,
        )
        if insert_index < 0:
            raise AppException("插入位置分镜组不存在", code=40410, status_code=404)

    shots = [_empty_agent_shot()]
    duration = estimate_agent_shot_group_duration(shots)
    prompt_template = await _build_prompt_template(db, production, shots, "", duration)
    storyboard = ProjectStoryboard(
        project_id=production.project_id,
        chapter_id=chapter.id,
        user_id=production.user_id,
        shot_number=insert_index + 1,
        title=payload.title,
        source_content=payload.source_content,
        characters=[],
        props=[],
        video_prompt=prompt_template,
        duration_suggestion=f"{duration}秒",
        extra={
            "agent_shots": shots,
            "agent_storyboard_prompt_template": prompt_template,
            "agent_storyboard_prompt": prompt_template,
            "agent_storyboard_prompt_notes": "",
            "agent_storyboard_revision": 1,
            "agent_storyboard_status": "draft",
            "agent_storyboard_validation_errors": ["分镜组镜头字段不完整"],
            "agent_storyboard_origin": "user",
            "agent_asset_ids": {key: [] for key in ("character", "scene", "prop")},
            "agent_asset_bindings": [],
            "agent_asset_binding_labels": {},
            "estimated_duration_seconds": duration,
        },
        is_enabled=True,
    )
    db.add(storyboard)
    await db.flush()
    final_order = storyboards[:insert_index] + [storyboard] + storyboards[insert_index:]
    _renumber_storyboards(final_order, skip_revision_for={storyboard.id})
    _bump_episode_revision(chapter)
    await _after_storyboard_structure_change(db, production, chapter, user, "storyboard.created", storyboard)
    await db.commit()
    await db.refresh(storyboard)
    return _storyboard_payload(storyboard)


async def copy_agent_storyboard(
    db: AsyncSession,
    production_id: UUID,
    storyboard_id: UUID,
    user: User,
    payload: AgentStoryboardCopyRequest,
) -> Dict[str, Any]:
    production = await _get_owned_production(db, production_id, user.id)
    _assert_storyboard_mutation_allowed(production)
    await _require_core_lock_version(
        db,
        production.id,
        payload.expected_core_asset_lock_version,
    )
    source, chapter = await _locked_storyboard(db, production, storyboard_id)
    await _require_storyboard_episode_ready(db, production, chapter.id)
    _check_storyboard_revision(source, payload.expected_revision)
    _assert_storyboard_idle(source)
    _, storyboards = await _locked_episode_storyboards(db, production, chapter.id)
    source_index = next(
        index for index, item in enumerate(storyboards) if item.id == source.id
    )
    copied = _copy_storyboard_group(source)
    copied.title = f"{source.title}（副本）"[:128]
    copied.shot_number = source_index + 2
    db.add(copied)
    await db.flush()
    final_order = storyboards[: source_index + 1] + [copied] + storyboards[source_index + 1 :]
    _renumber_storyboards(final_order, skip_revision_for={copied.id})
    _bump_episode_revision(chapter)
    await _after_storyboard_structure_change(db, production, chapter, user, "storyboard.copied", copied)
    await db.commit()
    await db.refresh(copied)
    return _storyboard_payload(copied)


async def delete_agent_storyboard(
    db: AsyncSession,
    production_id: UUID,
    storyboard_id: UUID,
    user: User,
    *,
    expected_core_asset_lock_version: int,
    expected_revision: int,
) -> Dict[str, Any]:
    production = await _get_owned_production(db, production_id, user.id)
    _assert_storyboard_mutation_allowed(production)
    await _require_core_lock_version(
        db,
        production.id,
        expected_core_asset_lock_version,
    )
    storyboard, chapter = await _locked_storyboard(db, production, storyboard_id)
    await _require_storyboard_episode_ready(db, production, chapter.id)
    _check_storyboard_revision(storyboard, expected_revision)
    _assert_storyboard_idle(storyboard)
    _, storyboards = await _locked_episode_storyboards(db, production, chapter.id)
    if len(storyboards) <= 1:
        raise AppException(
            "每集至少需要保留一个分镜组，请先新增分镜组后再删除",
            code=40969,
            status_code=409,
        )
    storyboard.is_enabled = False
    storyboard.updated_at = beijing_datetime()
    await _invalidate_storyboard_media(db, storyboard, {"image", "video"})
    _renumber_storyboards([item for item in storyboards if item.id != storyboard.id])
    _bump_episode_revision(chapter)
    await _after_storyboard_structure_change(db, production, chapter, user, "storyboard.deleted", storyboard)
    await db.commit()
    return {
        "id": storyboard.id,
        "chapter_id": chapter.id,
        "deleted": True,
        "episode_revision": _episode_revision(chapter),
    }


async def reorder_agent_storyboards(
    db: AsyncSession,
    production_id: UUID,
    chapter_id: UUID,
    user: User,
    payload: AgentStoryboardReorderRequest,
) -> Dict[str, Any]:
    production = await _get_owned_production(db, production_id, user.id)
    _assert_storyboard_mutation_allowed(production)
    chapter, storyboards = await _locked_episode_storyboards(
        db,
        production,
        chapter_id,
    )
    await _require_storyboard_episode_ready(db, production, chapter.id)
    _check_episode_revision(chapter, payload.expected_episode_revision)
    by_id = {item.id: item for item in storyboards}
    if set(payload.storyboard_ids) != set(by_id):
        raise AppException(
            "排序必须提交本集全部有效分镜组",
            code=40960,
            status_code=409,
        )
    ordered = [by_id[item_id] for item_id in payload.storyboard_ids]
    changed = any(item.shot_number != index for index, item in enumerate(ordered, start=1))
    if changed:
        _renumber_storyboards(ordered)
        _bump_episode_revision(chapter)
        production.lock_version += 1
        await _invalidate_episode_review(db, production.id, chapter.id)
        db.add(
            AgentEvent(
                production_id=production.id,
                actor_user_id=user.id,
                event_type="storyboard.reordered",
                source="user",
                payload={
                    "chapter_id": str(chapter.id),
                    "storyboard_ids": [str(value) for value in payload.storyboard_ids],
                },
            )
        )
    await db.commit()
    return {
        "chapter_id": chapter.id,
        "episode_revision": _episode_revision(chapter),
        "storyboards": [_storyboard_payload(item) for item in ordered],
    }


def _assert_storyboard_mutation_allowed(production: AgentProduction) -> None:
    if production.status in {"completed", "cancelled"}:
        raise AppException("当前整剧状态不允许修改分镜", code=40956, status_code=409)


async def _require_core_lock_version(
    db: AsyncSession,
    production_id: UUID,
    expected_version: int,
) -> AgentCoreAssetLock:
    core_lock = await _active_core_lock(db, production_id)
    if core_lock is None:
        raise AppException("核心资产尚未锁定", code=40950, status_code=409)
    if core_lock.version != expected_version:
        raise AppException(
            f"核心资产锁版本冲突，当前版本为 {core_lock.version}",
            code=40955,
            status_code=409,
            data={
                "expected_core_asset_lock_version": expected_version,
                "current_core_asset_lock_version": core_lock.version,
            },
        )
    return core_lock


async def _locked_episode_storyboards(
    db: AsyncSession,
    production: AgentProduction,
    chapter_id: UUID,
) -> tuple[ProjectChapter, List[ProjectStoryboard]]:
    chapter_result = await db.execute(
        select(ProjectChapter)
        .where(
            ProjectChapter.id == chapter_id,
            ProjectChapter.project_id == production.project_id,
            ProjectChapter.user_id == production.user_id,
            ProjectChapter.is_enabled.is_(True),
            ProjectChapter.extra["agent_production_id"].as_string()
            == str(production.id),
        )
        .with_for_update(of=ProjectChapter)
    )
    chapter = chapter_result.scalar_one_or_none()
    if chapter is None:
        raise AppException("Agent 分集不存在", code=40411, status_code=404)
    storyboard_result = await db.execute(
        select(ProjectStoryboard)
        .where(
            ProjectStoryboard.project_id == production.project_id,
            ProjectStoryboard.chapter_id == chapter.id,
            ProjectStoryboard.user_id == production.user_id,
            ProjectStoryboard.is_enabled.is_(True),
        )
        .order_by(
            ProjectStoryboard.shot_number,
            ProjectStoryboard.created_at,
            ProjectStoryboard.id,
        )
        .with_for_update(of=ProjectStoryboard)
    )
    return chapter, list(storyboard_result.scalars().all())


def _storyboard_revision(storyboard: ProjectStoryboard) -> int:
    return max(1, int((storyboard.extra or {}).get("agent_storyboard_revision") or 1))


def _episode_revision(chapter: ProjectChapter) -> int:
    return max(1, int((chapter.extra or {}).get("agent_storyboard_episode_revision") or 1))


def _check_storyboard_revision(storyboard: ProjectStoryboard, expected: int) -> None:
    current = _storyboard_revision(storyboard)
    if expected != current:
        raise AppException(
            f"分镜组版本冲突，当前版本为 {current}",
            code=40959,
            status_code=409,
            data={"expected_revision": expected, "current_revision": current},
        )


def _check_episode_revision(chapter: ProjectChapter, expected: Optional[int]) -> None:
    if expected is None:
        return
    current = _episode_revision(chapter)
    if expected != current:
        raise AppException(
            f"分集分镜版本冲突，当前版本为 {current}",
            code=40960,
            status_code=409,
            data={"expected_episode_revision": expected, "current_episode_revision": current},
        )


def _bump_episode_revision(chapter: ProjectChapter) -> None:
    chapter.extra = {
        **(chapter.extra or {}),
        "agent_storyboard_episode_revision": _episode_revision(chapter) + 1,
    }


def _renumber_storyboards(
    storyboards: List[ProjectStoryboard],
    *,
    skip_revision_for: Optional[set[UUID]] = None,
) -> None:
    skipped = skip_revision_for or set()
    now = beijing_datetime()
    for index, storyboard in enumerate(storyboards, start=1):
        if storyboard.shot_number == index:
            continue
        storyboard.shot_number = index
        extra = dict(storyboard.extra or {})
        if storyboard.id not in skipped:
            extra["agent_storyboard_revision"] = _storyboard_revision(storyboard) + 1
        storyboard.extra = extra
        storyboard.updated_at = now


def _assert_storyboard_idle(storyboard: ProjectStoryboard) -> None:
    extra = storyboard.extra or {}
    if any(
        str(extra.get(f"{media_type}_generation_status") or "") in ACTIVE_STATUSES
        for media_type in ("image", "video")
    ):
        raise AppException("分镜正在生成媒体，暂时不能修改", code=40956, status_code=409)


async def _after_storyboard_structure_change(
    db: AsyncSession,
    production: AgentProduction,
    chapter: ProjectChapter,
    user: User,
    event_type: str,
    storyboard: ProjectStoryboard,
) -> None:
    await _reopen_batch_for_storyboard_edit(db, production, "images")
    await _invalidate_episode_review(db, production.id, chapter.id)
    production.lock_version += 1
    db.add(
        AgentEvent(
            production_id=production.id,
            actor_user_id=user.id,
            event_type=event_type,
            source="user",
            payload={
                "storyboard_id": str(storyboard.id),
                "chapter_id": str(chapter.id),
                "episode_revision": _episode_revision(chapter),
            },
        )
    )


async def _invalidate_episode_review(
    db: AsyncSession,
    production_id: UUID,
    chapter_id: UUID,
) -> None:
    await db.execute(
        update(AgentEpisodeReview)
        .where(
            AgentEpisodeReview.production_id == production_id,
            AgentEpisodeReview.chapter_id == chapter_id,
            AgentEpisodeReview.status == "approved",
        )
        .values(status="invalidated")
    )


def _empty_agent_shot() -> Dict[str, Any]:
    return {
        "shot_number": 1,
        "shot_size": "",
        "camera_shot": "",
        "camera_angle": "",
        "camera_movement": "",
        "visual_content": "",
        "scene_name": "",
        "characters": [],
        "props": [],
        "speaker": "",
        "dialogue": "",
        "character_binding_keys": [],
        "scene_binding_key": None,
        "prop_binding_keys": [],
    }


def _storyboard_validation(
    shots: List[Dict[str, Any]],
    duration: int,
    valid_binding_keys: Optional[set[str]] = None,
) -> tuple[str, List[str]]:
    errors: List[str] = []
    if not shots:
        errors.append("分镜组必须包含至少一个镜头")
    required = (
        "shot_size",
        "camera_shot",
        "camera_angle",
        "camera_movement",
        "visual_content",
    )
    if shots and any(not str(shot.get(key) or "").strip() for shot in shots for key in required):
        errors.append("分镜组镜头字段不完整")
    if not 4 <= duration <= 15:
        errors.append("分镜组预估时长必须在 4-15 秒之间")
    if valid_binding_keys is not None and any(
        _shot_has_invalid_asset_reference(shot, valid_binding_keys) for shot in shots
    ):
        errors.append("分镜组存在未绑定或已失效的资产引用")
    if not errors:
        return "ready", []
    if errors == ["分镜组镜头字段不完整"] or not shots:
        return "draft", errors
    return "invalid", errors


def _shot_has_invalid_asset_reference(
    shot: Dict[str, Any],
    valid_binding_keys: set[str],
) -> bool:
    referenced = [
        *(shot.get("character_binding_keys") or []),
        *(shot.get("prop_binding_keys") or []),
        *([shot.get("scene_binding_key")] if shot.get("scene_binding_key") else []),
    ]
    if any(str(key) not in valid_binding_keys for key in referenced):
        return True
    visible_names = [
        *(shot.get("characters") or []),
        *(shot.get("props") or []),
        *([shot.get("scene_name")] if shot.get("scene_name") else []),
    ]
    return any(
        str(name).strip() and "{{asset:" not in str(name) for name in visible_names
    )


async def _build_prompt_template(
    db: AsyncSession,
    production: AgentProduction,
    shots: List[Dict[str, Any]],
    notes: str,
    duration: int,
) -> str:
    result = await db.execute(
        select(Style.prompt)
        .join(Project, Project.style_id == Style.id)
        .where(Project.id == production.project_id)
    )
    visual_style = str(result.scalar_one_or_none() or "")
    prompt = build_agent_storyboard_prompt(visual_style, shots, duration)
    if notes:
        prompt += f"\n补充要求：{notes}"
    return prompt


def _apply_shot_summary(
    storyboard: ProjectStoryboard,
    shots: List[Dict[str, Any]],
) -> None:
    first = shots[0] if shots else {}
    storyboard.scene_name = str(first.get("scene_name") or "")[:128] or None
    storyboard.shot_size = str(first.get("shot_size") or "")[:64] or None
    storyboard.camera_angle = str(first.get("camera_angle") or "")[:128] or None
    storyboard.camera_movement = str(first.get("camera_movement") or "") or None
    storyboard.screen_execution = str(first.get("visual_content") or "") or None
    storyboard.action = "\n".join(
        str(item.get("visual_content") or "").strip()
        for item in shots
        if str(item.get("visual_content") or "").strip()
    ) or None
    storyboard.dialogue = "\n".join(
        str(item.get("dialogue") or "").strip()
        for item in shots
        if str(item.get("dialogue") or "").strip()
    ) or None
    storyboard.characters = _ordered_shot_strings(shots, "characters")
    storyboard.props = _ordered_shot_strings(shots, "props")


def _ordered_shot_strings(shots: List[Dict[str, Any]], key: str) -> List[str]:
    values: List[str] = []
    for shot in shots:
        for raw in shot.get(key) or []:
            value = str(raw).strip()
            if value and value not in values:
                values.append(value)
    return values


def _copy_storyboard_group(source: ProjectStoryboard) -> ProjectStoryboard:
    extra = deepcopy(source.extra or {})
    for key in list(extra):
        if key.startswith(("image_generation_", "video_generation_")) or key in {
            "task_record_id",
            "image_reference_asset_ids",
        }:
            extra.pop(key, None)
    extra.update(
        {
            "agent_storyboard_revision": 1,
            "agent_storyboard_origin": "copy",
            "agent_storyboard_origin_id": str(source.id),
        }
    )
    return ProjectStoryboard(
        project_id=source.project_id,
        chapter_id=source.chapter_id,
        user_id=source.user_id,
        ai_model_id=source.ai_model_id,
        shot_number=source.shot_number + 1,
        title=source.title,
        source_content=source.source_content,
        scene_name=source.scene_name,
        scene_state=source.scene_state,
        shot_size=source.shot_size,
        camera_angle=source.camera_angle,
        camera_movement=source.camera_movement,
        screen_execution=source.screen_execution,
        characters=list(source.characters or []),
        props=list(source.props or []),
        action=source.action,
        character_action=source.character_action,
        character_expression=source.character_expression,
        dialogue=source.dialogue,
        sound_effect=source.sound_effect,
        atmosphere=source.atmosphere,
        image_prompt=source.image_prompt,
        video_prompt=source.video_prompt,
        duration_suggestion=source.duration_suggestion,
        production_focus=source.production_focus,
        negative_prompt=source.negative_prompt,
        ending_frame=source.ending_frame,
        extra=extra,
        is_enabled=True,
    )


def _binding_labels(extra: Dict[str, Any]) -> Dict[str, str]:
    stored = extra.get("agent_asset_binding_labels") or {}
    return {
        str(key): str(value)
        for key, value in stored.items()
        if str(key) and str(value)
    }


def _label_to_key(labels: Dict[str, str]) -> Dict[str, str]:
    return {label: key for key, label in labels.items() if label}


def _serialize_bindings(bindings: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    return [
        {
            **(
                {"binding_key": str(item["binding_key"])}
                if item.get("binding_key")
                else {}
            ),
            "asset_type": str(item["asset_type"]),
            "asset_id": str(item["asset_id"]),
            **(
                {"variant_id": str(item["variant_id"])}
                if item.get("variant_id") is not None
                else {}
            ),
        }
        for item in bindings
    ]


def _store_binding_state(
    extra: Dict[str, Any],
    core_lock: AgentCoreAssetLock,
    bindings: List[Dict[str, str]],
    variant_context: List[Dict[str, str]],
    labels: Dict[str, str],
) -> None:
    bound = {asset_type: [] for asset_type in ("character", "scene", "prop")}
    variant_ids: Dict[str, Dict[str, str]] = {
        asset_type: {} for asset_type in ("character", "scene", "prop")
    }
    for item in bindings:
        bound[item["asset_type"]].append(item["asset_id"])
        if item.get("variant_id"):
            variant_ids[item["asset_type"]][item["asset_id"]] = item["variant_id"]
    extra.update(
        {
            "agent_asset_ids": bound,
            "agent_asset_variant_ids": variant_ids,
            "agent_asset_bindings": bindings,
            "agent_asset_variant_context": variant_context,
            "agent_asset_binding_labels": labels,
            "agent_unbound_asset_names": {
                "character": [],
                "scene": [],
                "prop": [],
            },
            "agent_core_asset_lock_id": str(core_lock.id),
            "agent_core_asset_lock_version": core_lock.version,
            "agent_asset_binding_version": AGENT_ASSET_BINDING_VERSION,
            "agent_asset_binding_source": "user",
        }
    )


def _next_binding_key(asset_type: str, used_keys: set[str]) -> str:
    index = 1
    while f"{asset_type}_{index}" in used_keys:
        index += 1
    return f"{asset_type}_{index}"


async def _active_core_lock(
    db: AsyncSession,
    production_id: UUID,
) -> Optional[AgentCoreAssetLock]:
    result = await db.execute(
        select(AgentCoreAssetLock)
        .where(
            AgentCoreAssetLock.production_id == production_id,
            AgentCoreAssetLock.status == "active",
        )
        .order_by(AgentCoreAssetLock.version.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _locked_storyboard(
    db: AsyncSession,
    production: AgentProduction,
    storyboard_id: UUID,
) -> tuple[ProjectStoryboard, ProjectChapter]:
    result = await db.execute(
        select(ProjectStoryboard, ProjectChapter)
        .join(ProjectChapter, ProjectChapter.id == ProjectStoryboard.chapter_id)
        .where(
            ProjectStoryboard.id == storyboard_id,
            ProjectStoryboard.project_id == production.project_id,
            ProjectStoryboard.user_id == production.user_id,
            ProjectStoryboard.is_enabled.is_(True),
            ProjectChapter.extra["agent_production_id"].as_string()
            == str(production.id),
        )
        .with_for_update(of=ProjectStoryboard)
    )
    row = result.one_or_none()
    if row is None:
        raise AppException("Agent 分镜不存在", code=40410, status_code=404)
    return row[0], row[1]


async def _validate_asset_bindings(
    db: AsyncSession,
    production: AgentProduction,
    core_lock: AgentCoreAssetLock,
    chapter: ProjectChapter,
    requested: List[AgentStoryboardAssetBinding],
    current: List[Dict[str, Any]],
) -> tuple[List[Dict[str, str]], List[Dict[str, str]], Dict[str, str]]:
    allowed: Dict[tuple[str, UUID], Dict[str, Any]] = {}
    for raw in core_lock.assets or []:
        asset_type = str(raw.get("asset_type") or "")
        asset_id = _optional_uuid(raw.get("asset_id"))
        if asset_type in {"character", "scene", "prop"} and asset_id is not None:
            allowed[(asset_type, asset_id)] = raw

    variant_ids = [item.variant_id for item in requested if item.variant_id is not None]
    variants: Dict[UUID, AgentAssetVariant] = {}
    if variant_ids:
        result = await db.execute(
            select(AgentAssetVariant).where(
                AgentAssetVariant.id.in_(variant_ids),
                AgentAssetVariant.production_id == production.id,
                AgentAssetVariant.bible_version_id == core_lock.bible_version_id,
                AgentAssetVariant.review_status == "ready",
            )
        )
        variants = {variant.id: variant for variant in result.scalars().all()}

    episode_number = int((chapter.extra or {}).get("episode_number") or 0)
    bindings: List[Dict[str, str]] = []
    variant_context: List[Dict[str, str]] = []
    labels: Dict[str, str] = {}
    current_by_type: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for item in current:
        current_by_type[str(item["asset_type"])].append(item)
    used_keys: set[str] = set()
    type_indexes: Dict[str, int] = defaultdict(int)
    for item in requested:
        snapshot = allowed.get((item.asset_type, item.asset_id))
        if snapshot is None:
            raise AppException(
                "分镜只能绑定当前已确认的核心资产",
                code=40957,
                status_code=409,
                data={"asset_type": item.asset_type, "asset_id": str(item.asset_id)},
            )
        type_index = type_indexes[item.asset_type]
        type_indexes[item.asset_type] += 1
        binding_key = item.binding_key
        if binding_key is None:
            exact = next(
                (
                    value
                    for value in current_by_type[item.asset_type]
                    if value["asset_id"] == item.asset_id
                    and value.get("variant_id") == item.variant_id
                ),
                None,
            )
            positional = (
                current_by_type[item.asset_type][type_index]
                if type_index < len(current_by_type[item.asset_type])
                else None
            )
            binding_key = str(
                (exact or positional or {}).get("binding_key")
                or _next_binding_key(item.asset_type, used_keys)
            )
        if binding_key in used_keys or not binding_key.startswith(f"{item.asset_type}_"):
            raise AppException(
                "资产绑定槽位无效或重复",
                code=40961,
                status_code=409,
                data={"binding_key": binding_key, "asset_type": item.asset_type},
            )
        used_keys.add(binding_key)
        binding = {
            "binding_key": binding_key,
            "asset_type": item.asset_type,
            "asset_id": str(item.asset_id),
        }
        label = str(
            snapshot.get("name")
            or snapshot.get("canonical_name")
            or snapshot.get("asset_name")
            or item.asset_id
        )
        if item.variant_id is not None:
            variant = variants.get(item.variant_id)
            candidate_id = _optional_uuid(snapshot.get("candidate_id"))
            if (
                variant is None
                or candidate_id is None
                or variant.base_candidate_id != candidate_id
                or variant.asset_type != item.asset_type
                or (
                    variant.episode_numbers
                    and episode_number not in set(variant.episode_numbers)
                )
            ):
                raise AppException(
                    "资产变体不属于当前基础资产或不适用于本集",
                    code=40958,
                    status_code=409,
                    data={"variant_id": str(item.variant_id)},
                )
            binding["variant_id"] = str(variant.id)
            label = variant.canonical_name
            variant_context.append(
                {
                    "binding_key": binding_key,
                    "asset_type": item.asset_type,
                    "asset_id": str(item.asset_id),
                    "variant_id": str(variant.id),
                    "name": variant.canonical_name,
                    "variant_type": variant.variant_type,
                    "description": variant.description,
                    "trigger_reason": variant.trigger_reason,
                }
            )
        bindings.append(binding)
        labels[binding_key] = label
    return (
        sorted(bindings, key=lambda item: (item["asset_type"], item["binding_key"])),
        sorted(
            variant_context,
            key=lambda item: (item["asset_type"], item["binding_key"]),
        ),
        labels,
    )


async def _invalidate_storyboard_media(
    db: AsyncSession,
    storyboard: ProjectStoryboard,
    media_types: set[str],
) -> None:
    now = beijing_datetime()
    extra = dict(storyboard.extra or {})
    for media_type in media_types:
        status_key = f"{media_type}_generation_status"
        if extra.get(status_key) in SUCCESS_STATUSES | {"selection_required"}:
            extra[status_key] = "invalidated"
    storyboard.extra = extra
    result = await db.execute(
        select(ProjectGeneratedAsset).where(
            ProjectGeneratedAsset.target_type == "storyboard",
            ProjectGeneratedAsset.target_id == storyboard.id,
            ProjectGeneratedAsset.media_type.in_(tuple(media_types)),
            ProjectGeneratedAsset.is_enabled.is_(True),
        )
    )
    histories = list(result.scalars().all())
    for history in histories:
        history.extra = {
            **(history.extra or {}),
            "validity_status": "invalidated",
            "invalidated_by": "agent_storyboard_edit",
            "invalidated_at": now.isoformat(),
        }
    if "video" in media_types and any(
        history.media_type == "video" for history in histories
    ):
        extra = dict(storyboard.extra or {})
        extra["video_selection_required"] = True
        storyboard.extra = extra


async def _reopen_batch_for_storyboard_edit(
    db: AsyncSession,
    production: AgentProduction,
    target_phase: str,
) -> None:
    result = await db.execute(
        select(AgentStep)
        .where(
            AgentStep.production_id == production.id,
            AgentStep.stage == "batch_production",
            AgentStep.scope_type == "production",
        )
        .order_by(AgentStep.created_at.desc(), AgentStep.id.desc())
        .with_for_update(of=AgentStep)
        .limit(1)
    )
    step = result.scalar_one_or_none()
    if step is None:
        return
    phase = str((step.extra or {}).get("phase") or "not_started")
    phase_order = {
        "not_started": 0,
        "storyboards": 1,
        "images": 2,
        "videos": 3,
        "exceptions": 4,
        "completion_pending": 5,
        "completed": 6,
    }
    if phase_order.get(phase, 0) <= phase_order[target_phase]:
        return
    step.extra = {**(step.extra or {}), "phase": target_phase}
    production.current_stage = f"batch_{target_phase}"


async def _get_owned_production(
    db: AsyncSession,
    production_id: UUID,
    user_id: UUID,
) -> AgentProduction:
    result = await db.execute(
        select(AgentProduction)
        .join(Project, Project.id == AgentProduction.project_id)
        .where(
            AgentProduction.id == production_id,
            AgentProduction.user_id == user_id,
            Project.user_id == user_id,
            Project.is_enabled.is_(True),
        )
    )
    production = result.scalar_one_or_none()
    if production is None:
        raise AppException("整剧任务不存在", code=40430, status_code=404)
    return production


async def _production_chapters(
    db: AsyncSession,
    production: AgentProduction,
) -> List[ProjectChapter]:
    result = await db.execute(
        select(ProjectChapter)
        .where(
            ProjectChapter.project_id == production.project_id,
            ProjectChapter.user_id == production.user_id,
            ProjectChapter.is_enabled.is_(True),
            ProjectChapter.extra["agent_production_id"].as_string() == str(production.id),
        )
        .order_by(ProjectChapter.sort_order, ProjectChapter.created_at, ProjectChapter.id)
    )
    return list(result.scalars().all())


async def _production_storyboards(
    db: AsyncSession,
    production: AgentProduction,
    chapters: List[ProjectChapter],
) -> List[ProjectStoryboard]:
    if not chapters:
        return []
    result = await db.execute(
        select(ProjectStoryboard)
        .where(
            ProjectStoryboard.project_id == production.project_id,
            ProjectStoryboard.user_id == production.user_id,
            ProjectStoryboard.chapter_id.in_([chapter.id for chapter in chapters]),
            ProjectStoryboard.is_enabled.is_(True),
        )
        .order_by(
            ProjectStoryboard.chapter_id,
            ProjectStoryboard.shot_number,
            ProjectStoryboard.created_at,
            ProjectStoryboard.id,
        )
    )
    return list(result.scalars().all())


async def _require_storyboard_episode_ready(
    db: AsyncSession,
    production: AgentProduction,
    chapter_id: UUID,
) -> None:
    chapters = await _production_chapters(db, production)
    storyboards = await _production_storyboards(db, production, chapters)
    storyboard_counts: Dict[UUID, int] = defaultdict(int)
    for storyboard in storyboards:
        storyboard_counts[storyboard.chapter_id] += 1
    progress = build_storyboard_episode_progress(chapters, storyboard_counts)
    require_visible_storyboard_episode(progress, chapter_id)


def _storyboard_payload(storyboard: ProjectStoryboard) -> Dict[str, Any]:
    extra = storyboard.extra or {}
    labels = _binding_labels(extra)
    prompt = str(extra.get("agent_storyboard_prompt") or storyboard.video_prompt or "")
    prompt_template = str(extra.get("agent_storyboard_prompt_template") or prompt)
    return {
        "id": storyboard.id,
        "chapter_id": storyboard.chapter_id,
        "shot_number": storyboard.shot_number,
        "group_number": storyboard.shot_number,
        "revision": _storyboard_revision(storyboard),
        "status": str(extra.get("agent_storyboard_status") or "ready"),
        "origin": str(extra.get("agent_storyboard_origin") or "model"),
        "title": storyboard.title,
        "source_content": storyboard.source_content,
        "event_goal": storyboard.event_goal,
        "scene_name": storyboard.scene_name,
        "scene_state": storyboard.scene_state,
        "characters": list(storyboard.characters or []),
        "props": list(storyboard.props or []),
        "shot_size": storyboard.shot_size,
        "camera_angle": storyboard.camera_angle,
        "camera_movement": storyboard.camera_movement,
        "screen_execution": storyboard.screen_execution,
        "action": storyboard.action,
        "character_action": storyboard.character_action,
        "character_expression": storyboard.character_expression,
        "dialogue": storyboard.dialogue,
        "sound_effect": storyboard.sound_effect,
        "atmosphere": storyboard.atmosphere,
        "shots": render_storyboard_shots(list(extra.get("agent_shots") or []), labels),
        "storyboard_prompt": prompt,
        "prompt_template": prompt_template,
        "effective_prompt": prompt,
        "prompt_notes": str(extra.get("agent_storyboard_prompt_notes") or ""),
        "estimated_duration_seconds": storyboard_estimated_duration_seconds(storyboard),
        "duration_source": str(extra.get("agent_duration_source") or "model"),
        "video_config": dict(extra.get("agent_video_config") or {}),
        "asset_ids": _storyboard_asset_ids(storyboard),
        "asset_bindings": _storyboard_asset_bindings(storyboard),
        "production_focus": storyboard.production_focus,
        "ending_frame": storyboard.ending_frame,
        "validation_errors": list(
            extra.get("agent_storyboard_validation_errors") or []
        ),
        "updated_at": storyboard.updated_at,
    }


def _storyboard_asset_ids(storyboard: ProjectStoryboard) -> Dict[str, List[UUID]]:
    mapping = (storyboard.extra or {}).get("agent_asset_ids") or {}
    return {
        asset_type: [
            value
            for value in (_optional_uuid(item) for item in mapping.get(asset_type) or [])
            if value is not None
        ]
        for asset_type in ("character", "scene", "prop")
    }


def _storyboard_asset_bindings(storyboard: ProjectStoryboard) -> List[Dict[str, Any]]:
    extra = storyboard.extra or {}
    stored = extra.get("agent_asset_bindings")
    if isinstance(stored, list):
        result = []
        for item in stored:
            if not isinstance(item, dict):
                continue
            asset_type = str(item.get("asset_type") or "")
            binding_key = str(item.get("binding_key") or "")
            asset_id = _optional_uuid(item.get("asset_id"))
            variant_id = _optional_uuid(item.get("variant_id"))
            if asset_type not in {"character", "scene", "prop"} or asset_id is None:
                continue
            result.append(
                {
                    **({"binding_key": binding_key} if binding_key else {}),
                    "asset_type": asset_type,
                    "asset_id": asset_id,
                    **({"variant_id": variant_id} if variant_id is not None else {}),
                }
            )
        return sorted(
            result,
            key=lambda item: (
                item["asset_type"],
                str(item.get("binding_key") or item["asset_id"]),
            ),
        )

    variant_mapping = extra.get("agent_asset_variant_ids") or {}
    result = []
    for asset_type, asset_ids in _storyboard_asset_ids(storyboard).items():
        typed_variants = variant_mapping.get(asset_type) or {}
        for index, asset_id in enumerate(asset_ids, start=1):
            variant_id = _optional_uuid(typed_variants.get(str(asset_id)))
            result.append(
                {
                    "binding_key": f"{asset_type}_{index}",
                    "asset_type": asset_type,
                    "asset_id": asset_id,
                    **({"variant_id": variant_id} if variant_id is not None else {}),
                }
            )
    return sorted(
        result,
        key=lambda item: (item["asset_type"], str(item["binding_key"])),
    )


def _optional_uuid(value: Any) -> Optional[UUID]:
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None
