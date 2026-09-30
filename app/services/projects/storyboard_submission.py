"""分镜生成任务提交及输入准备；业务状态与 Outbox 同事务提交后尝试发布。"""

import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException
from app.models.agent_story_bible import AgentAssetCandidate, AgentAssetVariant
from app.models.project_asset import ProjectCharacter, ProjectProp, ProjectScene
from app.models.project_chapter import ProjectChapter
from app.models.project_storyboard import ProjectStoryboard
from app.models.task_record import UserTaskRecord
from app.models.user import User
from app.schemas.project_storyboard import (
    ProjectStoryboardAnalyzeRequest,
    ProjectStoryboardPromptRequest,
    ProjectStoryboardRefineRequest,
)
from app.services.generation.task_dispatch import (
    dispatch_tasks_best_effort,
    enqueue_task_dispatch,
)
from app.services.billing.model_points import (
    calculate_text_submission_points_cost,
    ensure_model_minimum_balance,
)
from app.services.billing.points import consume_user_points
from app.services.models.catalog import get_enabled_text_model_or_404
from app.services.projects.chapters import get_project_chapter_or_404
from app.services.prompts import render_system_prompt
from app.services.projects.queries import (
    get_owned_enabled_project_with_style_or_404,
    get_project_or_404,
)
from app.services.generation.task_records import (
    create_user_task_record,
)
from app.services.generation.text_inputs import normalize_text_analysis_extra
from app.services.projects.storyboard_parsing import (
    _parse_uuid,
)
from app.services.projects.storyboards import (
    list_enabled_storyboards,
    get_project_storyboard_or_404,
)


async def submit_storyboard_refinement(
    db: AsyncSession,
    project_id: UUID,
    chapter_id: UUID,
    storyboard_id: UUID,
    user: User,
    payload: ProjectStoryboardRefineRequest,
) -> Tuple[UserTaskRecord, int]:
    chapter = await get_project_chapter_or_404(db, project_id, chapter_id, user.id)
    storyboard = await get_project_storyboard_or_404(
        db,
        project_id=project_id,
        chapter_id=chapter_id,
        storyboard_id=storyboard_id,
        user_id=user.id,
    )
    storyboards = await list_enabled_storyboards(db, project_id, chapter_id, user.id)
    continuity_context = _storyboard_continuity_context(storyboards, storyboard)

    prompt = render_system_prompt(
        "storyboard_refinement.md",
        storyboard_unit=json.dumps(_storyboard_unit_payload(storyboard), ensure_ascii=False),
        previous_storyboard=json.dumps(continuity_context.get("previous"), ensure_ascii=False),
        next_storyboard=json.dumps(continuity_context.get("next"), ensure_ascii=False),
        characters=await _dump_storyboard_assets(db, ProjectCharacter, project_id, user.id),
        scenes=await _dump_storyboard_assets(db, ProjectScene, project_id, user.id),
        props=await _dump_storyboard_assets(db, ProjectProp, project_id, user.id),
    )
    return await _submit_storyboard_text_stage(
        db,
        project_id=project_id,
        chapter=chapter,
        user=user,
        ai_model_id=payload.ai_model_id,
        prompt=prompt,
        extra=payload.extra,
        storyboard=storyboard,
        generation_type="storyboard_refinement",
        title_prefix="分镜细化字段生成",
        status_key="storyboard_refinement_status",
        task_key="storyboard_refinement_task_record_id",
    )


async def submit_storyboard_image_prompt_generation(
    db: AsyncSession,
    project_id: UUID,
    chapter_id: UUID,
    storyboard_id: UUID,
    user: User,
    payload: ProjectStoryboardPromptRequest,
) -> Tuple[UserTaskRecord, int]:
    chapter = await get_project_chapter_or_404(db, project_id, chapter_id, user.id)
    storyboard = await get_project_storyboard_or_404(
        db,
        project_id=project_id,
        chapter_id=chapter_id,
        storyboard_id=storyboard_id,
        user_id=user.id,
    )
    prompt = render_system_prompt(
        "storyboard_image_prompt_generation.md",
        storyboard_unit=json.dumps(
            _storyboard_image_prompt_unit_payload(storyboard), ensure_ascii=False
        ),
        characters=await _dump_storyboard_assets(db, ProjectCharacter, project_id, user.id),
        scenes=await _dump_storyboard_assets(db, ProjectScene, project_id, user.id),
        props=await _dump_storyboard_assets(db, ProjectProp, project_id, user.id),
    )
    return await _submit_storyboard_text_stage(
        db,
        project_id=project_id,
        chapter=chapter,
        user=user,
        ai_model_id=payload.ai_model_id,
        prompt=prompt,
        extra=payload.extra,
        storyboard=storyboard,
        generation_type="storyboard_image_prompt",
        title_prefix="故事板提示词生成",
        status_key="storyboard_image_prompt_generation_status",
        task_key="storyboard_image_prompt_generation_task_record_id",
    )


async def submit_storyboard_analysis(
    db: AsyncSession,
    project_id: UUID,
    chapter_id: UUID,
    user: User,
    payload: ProjectStoryboardAnalyzeRequest,
    *,
    agent_context: Optional[Dict[str, object]] = None,
) -> Tuple[UserTaskRecord, int]:
    agent_project = None
    if agent_context:
        agent_project = await get_owned_enabled_project_with_style_or_404(db, project_id, user.id)
    else:
        await get_project_or_404(db, project_id, user.id)
    chapter = await get_project_chapter_or_404(db, project_id, chapter_id, user.id)
    if not chapter.processed_content:
        raise AppException("章节还没有处理后的内容，无法分析分镜", code=40011, status_code=400)

    ai_model = await get_enabled_text_model_or_404(db, payload.ai_model_id)
    await ensure_model_minimum_balance(db, user.id, ai_model)
    points_cost = calculate_text_submission_points_cost(ai_model)
    points_transaction = None
    if points_cost > 0:
        points_transaction = await consume_user_points(
            db,
            user_id=user.id,
            amount=points_cost,
            remark=f"项目章节分镜分析：{chapter.title}",
            auto_commit=False,
        )

    model_extra = normalize_text_analysis_extra(payload.extra)
    characters = await _dump_storyboard_assets(
        db, ProjectCharacter, project_id, user.id, chapter, agent_context
    )
    scenes = await _dump_storyboard_assets(
        db, ProjectScene, project_id, user.id, chapter, agent_context
    )
    props = await _dump_storyboard_assets(
        db, ProjectProp, project_id, user.id, chapter, agent_context
    )
    custom_system_prompt = (payload.analysis_prompt or "").strip()
    if custom_system_prompt:
        model_extra["system_prompt"] = custom_system_prompt
        prompt = _build_storyboard_analysis_input_prompt(
            input_text=chapter.processed_content,
            characters=characters,
            scenes=scenes,
            props=props,
        )
        prompt_source = "custom"
    elif agent_context:
        prompt = render_system_prompt(
            "agent_storyboard_generation.md",
            input_text=chapter.processed_content,
            characters=characters,
            scenes=scenes,
            props=props,
            visual_style=agent_project.style.prompt
            if agent_project is not None and agent_project.style is not None
            else "",
        )
        prompt_source = "agent"
    else:
        prompt = render_system_prompt(
            "storyboard_analysis.md",
            input_text=chapter.processed_content,
            characters=characters,
            scenes=scenes,
            props=props,
        )
        prompt_source = "system"
    storyboard_input_fingerprint = (
        hashlib.sha256(prompt.encode("utf-8")).hexdigest() if agent_context else None
    )
    task_record = await create_user_task_record(
        db,
        user_id=user.id,
        ai_model_id=ai_model.id,
        points_transaction_id=points_transaction.id if points_transaction else None,
        business_type="project",
        business_id=project_id,
        generation_type="storyboard_analysis",
        status="pending",
        title=f"分镜分析：{chapter.title}",
        prompt=prompt,
        result=None,
        points_cost=points_cost,
        extra={
            "project_id": str(project_id),
            "chapter_id": str(chapter_id),
            "chapter_title": chapter.title,
            "prompt_source": prompt_source,
            "model_extra": model_extra,
            **(
                {
                    "agent_visual_style": agent_project.style.prompt
                    if agent_project.style is not None
                    else ""
                }
                if agent_project is not None
                else {}
            ),
            **({"analysis_prompt": custom_system_prompt} if custom_system_prompt else {}),
            **(
                {"storyboard_analysis_input_fingerprint": storyboard_input_fingerprint}
                if storyboard_input_fingerprint is not None
                else {}
            ),
            **(agent_context or {}),
        },
    )
    await db.flush()
    chapter.extra = {
        **(chapter.extra or {}),
        "storyboard_analysis_status": "pending",
        "storyboard_analysis_task_record_id": str(task_record.id),
        **(
            {
                "storyboard_analysis_input_fingerprint": storyboard_input_fingerprint,
            }
            if storyboard_input_fingerprint is not None
            else {}
        ),
    }
    dispatch_id = await enqueue_task_dispatch(
        db,
        task_name="tasks.project_storyboard.run_project_storyboard_analysis",
        args=(str(task_record.id), str(chapter_id)),
        queue="story_ai_text",
        message_id=task_record.id,
    )
    await db.commit()

    await dispatch_tasks_best_effort(db, [dispatch_id])
    return task_record, points_cost


async def _submit_storyboard_text_stage(
    db: AsyncSession,
    *,
    project_id: UUID,
    chapter: ProjectChapter,
    user: User,
    ai_model_id: UUID,
    prompt: str,
    extra: Optional[Dict[str, Any]],
    storyboard: Optional[ProjectStoryboard],
    generation_type: str,
    title_prefix: str,
    status_key: str,
    task_key: str,
) -> Tuple[UserTaskRecord, int]:
    ai_model = await get_enabled_text_model_or_404(db, ai_model_id)
    await ensure_model_minimum_balance(db, user.id, ai_model)
    points_cost = calculate_text_submission_points_cost(ai_model)
    points_transaction = None
    if points_cost > 0:
        points_transaction = await consume_user_points(
            db,
            user_id=user.id,
            amount=points_cost,
            remark=_storyboard_stage_points_remark(title_prefix, chapter, storyboard),
            auto_commit=False,
        )

    model_extra = normalize_text_analysis_extra(extra)
    task_title = _storyboard_stage_task_title(title_prefix, chapter, storyboard)
    task_record = await create_user_task_record(
        db,
        user_id=user.id,
        ai_model_id=ai_model.id,
        points_transaction_id=points_transaction.id if points_transaction else None,
        business_type="project",
        business_id=project_id,
        generation_type=generation_type,
        status="pending",
        title=task_title,
        prompt=prompt,
        result=None,
        points_cost=points_cost,
        extra={
            "project_id": str(project_id),
            "chapter_id": str(chapter.id),
            "chapter_title": chapter.title,
            **_storyboard_stage_extra(storyboard),
            "model_extra": model_extra,
        },
    )
    await db.flush()
    chapter.extra = {
        **(chapter.extra or {}),
        status_key: "pending",
        task_key: str(task_record.id),
    }
    if storyboard is not None:
        storyboard.extra = {
            **(storyboard.extra or {}),
            status_key: "pending",
            task_key: str(task_record.id),
        }
    dispatch_id = await enqueue_task_dispatch(
        db,
        task_name="tasks.project_storyboard.run_project_storyboard_stage",
        args=(str(task_record.id), str(chapter.id)),
        queue="story_ai_text",
        message_id=task_record.id,
    )
    await db.commit()

    await dispatch_tasks_best_effort(db, [dispatch_id])
    return task_record, points_cost


def _build_storyboard_analysis_input_prompt(
    *,
    input_text: str,
    characters: str,
    scenes: str,
    props: str,
) -> str:
    return "\n\n".join(
        [
            "请根据系统规则完成分镜分析。以下是本次分析必须使用的输入内容，不得脱离这些输入扩写或编造剧情。",
            f"## 预处理文本\n{(input_text or '').strip()}",
            f"## 人物资产\n{(characters or '').strip()}",
            f"## 场景资产\n{(scenes or '').strip()}",
            f"## 道具资产\n{(props or '').strip()}",
            (
                "## 输出要求\n"
                "严格输出合法 JSON，不要输出 Markdown、代码块、注释或解释说明。"
                "JSON 顶层必须是对象，且必须包含 storyboard_units 数组；"
                "每个数组项必须包含 shot_number、title、source_content、event_goal、scene_name、"
                "characters、props、action、dialogue、split_reason。"
            ),
        ]
    )


async def _dump_storyboard_assets(
    db: AsyncSession,
    model: Any,
    project_id: UUID,
    user_id: UUID,
    chapter: Optional[ProjectChapter] = None,
    agent_context: Optional[Dict[str, object]] = None,
) -> str:
    result = await db.execute(
        select(model)
        .where(
            model.project_id == project_id,
            model.user_id == user_id,
            model.is_enabled.is_(True),
        )
        .order_by(model.created_at.asc())
    )
    model_assets = list(result.scalars().all())
    if agent_context and chapter is not None:
        model_assets = await _filter_agent_storyboard_assets(
            db,
            model_assets,
            agent_context,
            chapter,
        )
    assets = [_storyboard_asset_payload(asset) for asset in model_assets]
    if agent_context:
        await _append_agent_asset_variants(
            db,
            assets,
            model_assets,
            agent_context,
            chapter,
        )
    return json.dumps(assets, ensure_ascii=False)


async def _filter_agent_storyboard_assets(
    db: AsyncSession,
    assets: List[Any],
    agent_context: Dict[str, object],
    chapter: ProjectChapter,
) -> List[Any]:
    production_id = _parse_uuid(agent_context.get("agent_production_id"))
    if production_id is None or not assets:
        return assets
    result = await db.execute(
        select(AgentAssetCandidate).where(
            AgentAssetCandidate.production_id == production_id,
            AgentAssetCandidate.materialized_asset_id.in_([asset.id for asset in assets]),
        )
    )
    candidates = {
        candidate.materialized_asset_id: candidate
        for candidate in result.scalars().all()
        if candidate.materialized_asset_id is not None
    }
    episode_number = int((chapter.extra or {}).get("episode_number") or 0)
    return [
        asset
        for asset in assets
        if (candidate := candidates.get(asset.id)) is None
        or not candidate.episode_numbers
        or episode_number in candidate.episode_numbers
    ]


async def _append_agent_asset_variants(
    db: AsyncSession,
    payloads: List[Dict[str, Any]],
    assets: List[Any],
    agent_context: Dict[str, object],
    chapter: Optional[ProjectChapter],
) -> None:
    production_id = _parse_uuid(agent_context.get("agent_production_id"))
    asset_ids = [asset.id for asset in assets]
    if production_id is None or not asset_ids:
        return
    candidate_result = await db.execute(
        select(AgentAssetCandidate).where(
            AgentAssetCandidate.production_id == production_id,
            AgentAssetCandidate.materialized_asset_id.in_(asset_ids),
        )
    )
    candidates = list(candidate_result.scalars().all())
    if not candidates:
        return
    variant_result = await db.execute(
        select(AgentAssetVariant)
        .where(
            AgentAssetVariant.production_id == production_id,
            AgentAssetVariant.base_candidate_id.in_([item.id for item in candidates]),
            AgentAssetVariant.review_status != "rejected",
        )
        .order_by(AgentAssetVariant.created_at, AgentAssetVariant.id)
    )
    episode_number = int((chapter.extra or {}).get("episode_number") or 0) if chapter else 0
    variants_by_candidate: Dict[UUID, List[Dict[str, Any]]] = {}
    for variant in variant_result.scalars().all():
        episode_numbers = [int(value) for value in variant.episode_numbers or []]
        if episode_numbers and episode_number not in episode_numbers:
            continue
        variants_by_candidate.setdefault(variant.base_candidate_id, []).append(
            {
                "id": str(variant.id),
                "name": variant.canonical_name,
                "type": variant.variant_type,
                "description": variant.description,
                "trigger_reason": variant.trigger_reason,
                "content": variant.content or {},
            }
        )
    candidate_by_asset = {item.materialized_asset_id: item for item in candidates}
    for payload, asset in zip(payloads, assets):
        candidate = candidate_by_asset.get(asset.id)
        payload["variants"] = variants_by_candidate.get(candidate.id, []) if candidate else []


def _storyboard_asset_payload(asset: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "name": asset.name,
        "prompt": getattr(asset, "prompt", None) or "",
        "description": getattr(asset, "description", None) or "",
    }
    if isinstance(asset, ProjectCharacter):
        payload.update(
            {
                "aliases": asset.aliases or [],
                "identity": asset.identity or "",
                "appearance": asset.appearance or "",
            }
        )
    elif isinstance(asset, ProjectScene):
        payload.update(
            {
                "location": asset.location or "",
                "time_of_day": asset.time_of_day or "",
                "environment": asset.environment or "",
            }
        )
    elif isinstance(asset, ProjectProp):
        payload.update(
            {
                "category": asset.category or "",
                "appearance": asset.appearance or "",
                "function": asset.function or "",
            }
        )
    return payload


def _storyboard_unit_payload(storyboard: ProjectStoryboard) -> Dict[str, Any]:
    return {
        "shot_number": storyboard.shot_number,
        "title": storyboard.title,
        "source_content": storyboard.source_content,
        "event_goal": storyboard.event_goal or "",
        "scene_name": storyboard.scene_name or "",
        "characters": storyboard.characters or [],
        "props": storyboard.props or [],
        "action": storyboard.action or "",
        "dialogue": storyboard.dialogue or "",
        "split_reason": storyboard.split_reason or "",
    }


def _storyboard_image_prompt_unit_payload(storyboard: ProjectStoryboard) -> Dict[str, Any]:
    return {
        "shot_number": storyboard.shot_number,
        "title": storyboard.title,
        "source_content": storyboard.source_content,
    }


def _storyboard_continuity_context(
    storyboards: List[ProjectStoryboard],
    current: ProjectStoryboard,
) -> Dict[str, Any]:
    current_index = next(
        (index for index, item in enumerate(storyboards) if item.id == current.id), -1
    )
    previous_storyboard = storyboards[current_index - 1] if current_index > 0 else None
    next_storyboard = (
        storyboards[current_index + 1] if 0 <= current_index < len(storyboards) - 1 else None
    )
    return {
        "position": {
            "current_index": current_index + 1 if current_index >= 0 else current.shot_number,
            "total": len(storyboards),
            "is_first": current_index == 0,
            "is_last": current_index == len(storyboards) - 1,
        },
        "previous": _storyboard_continuity_payload(previous_storyboard),
        "next": _storyboard_continuity_payload(next_storyboard),
    }


def _storyboard_continuity_payload(
    storyboard: Optional[ProjectStoryboard],
) -> Optional[Dict[str, Any]]:
    if storyboard is None:
        return None
    return {
        "shot_number": storyboard.shot_number,
        "title": storyboard.title,
        "scene_name": storyboard.scene_name or "",
        "scene_state": storyboard.scene_state or "",
        "characters": storyboard.characters or [],
        "props": storyboard.props or [],
        "action": storyboard.action or "",
        "screen_execution": storyboard.screen_execution or "",
        "camera_movement": storyboard.camera_movement or "",
        "ending_frame": storyboard.ending_frame or "",
    }


def _storyboard_stage_extra(storyboard: Optional[ProjectStoryboard]) -> Dict[str, Any]:
    if storyboard is None:
        return {}
    return {
        "storyboard_id": str(storyboard.id),
        "shot_number": storyboard.shot_number,
        "storyboard_title": storyboard.title,
    }


def _storyboard_stage_task_title(
    title_prefix: str,
    chapter: ProjectChapter,
    storyboard: Optional[ProjectStoryboard],
) -> str:
    if storyboard is None:
        return f"{title_prefix}：{chapter.title}"
    return f"{title_prefix}：{chapter.title} / 第{storyboard.shot_number}镜"


def _storyboard_stage_points_remark(
    title_prefix: str,
    chapter: ProjectChapter,
    storyboard: Optional[ProjectStoryboard],
) -> str:
    if storyboard is None:
        return f"项目章节{title_prefix}：{chapter.title}"
    return f"项目分镜{title_prefix}：{chapter.title} / 第{storyboard.shot_number}镜"
