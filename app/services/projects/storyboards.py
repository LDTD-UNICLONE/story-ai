"""分镜读取、编辑与排序；编辑入口保留权限检查、章节锁和提交边界。"""

from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException
from app.core.timezone import beijing_datetime
from app.models.project_chapter import ProjectChapter
from app.models.project_generated_asset import ProjectGeneratedAsset
from app.models.project_storyboard import ProjectStoryboard
from app.models.task_record import UserTaskRecord
from app.schemas.project_storyboard import (
    ProjectStoryboardCreateRequest,
    ProjectStoryboardMergeRequest,
    ProjectStoryboardSplitRequest,
    ProjectStoryboardUpdateRequest,
)
from app.services.projects.chapters import get_project_chapter_or_404
from app.services.generation.task_records import (
    cancel_project_resource_task_records,
    expire_stale_task_record,
)
from app.services.projects.storyboard_parsing import (
    _as_int,
    _as_string_list,
    normalize_storyboard_item,
    _optional_str,
    _parse_uuid,
    _positive_duration_seconds,
)


async def list_project_storyboards(
    db: AsyncSession,
    project_id: UUID,
    chapter_id: UUID,
    user_id: UUID,
    page: int,
    page_size: int,
) -> Tuple[List[ProjectStoryboard], int]:
    await get_project_chapter_or_404(db, project_id, chapter_id, user_id)
    conditions = [
        ProjectStoryboard.project_id == project_id,
        ProjectStoryboard.chapter_id == chapter_id,
        ProjectStoryboard.user_id == user_id,
        ProjectStoryboard.is_enabled.is_(True),
    ]
    count_result = await db.execute(
        select(func.count()).select_from(ProjectStoryboard).where(*conditions)
    )
    total = count_result.scalar_one()
    result = await db.execute(
        select(ProjectStoryboard)
        .where(*conditions)
        .order_by(
            ProjectStoryboard.shot_number.asc(),
            ProjectStoryboard.created_at.asc(),
            ProjectStoryboard.id.asc(),
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(result.scalars().all()), total


async def get_project_storyboard_or_404(
    db: AsyncSession,
    project_id: UUID,
    chapter_id: UUID,
    storyboard_id: UUID,
    user_id: UUID,
) -> ProjectStoryboard:
    result = await db.execute(
        select(ProjectStoryboard).where(
            ProjectStoryboard.id == storyboard_id,
            ProjectStoryboard.project_id == project_id,
            ProjectStoryboard.chapter_id == chapter_id,
            ProjectStoryboard.user_id == user_id,
            ProjectStoryboard.is_enabled.is_(True),
        )
    )
    storyboard = result.scalar_one_or_none()
    if storyboard is None:
        raise AppException("项目分镜不存在", code=40410, status_code=404)
    await reconcile_storyboard_media_tasks(db, storyboard)
    return storyboard


async def reconcile_storyboard_media_tasks(db: AsyncSession, storyboard: ProjectStoryboard) -> None:
    await _reconcile_storyboard_media_task(
        db, storyboard, "image_generation_task_record_id", "image_generation_status"
    )
    await _reconcile_storyboard_media_task(
        db, storyboard, "video_generation_task_record_id", "video_generation_status"
    )


async def _reconcile_storyboard_media_task(
    db: AsyncSession,
    storyboard: ProjectStoryboard,
    task_key: str,
    status_key: str,
) -> None:
    task_record_id = (storyboard.extra or {}).get(task_key)
    status = (storyboard.extra or {}).get(status_key)
    if not task_record_id or status not in {"pending", "running"}:
        return
    parsed_task_record_id = _parse_uuid(task_record_id)
    if parsed_task_record_id is None:
        return
    task_record = await db.get(UserTaskRecord, parsed_task_record_id)
    if task_record is None:
        return
    await expire_stale_task_record(db, task_record)
    await db.refresh(storyboard)


async def create_project_storyboard(
    db: AsyncSession,
    project_id: UUID,
    chapter_id: UUID,
    user_id: UUID,
    payload: ProjectStoryboardCreateRequest,
) -> ProjectStoryboard:
    await get_project_chapter_or_404(db, project_id, chapter_id, user_id)
    await _lock_storyboard_order(db, project_id, chapter_id, user_id)
    storyboards = await list_enabled_storyboards(db, project_id, chapter_id, user_id)
    insert_index = _resolve_storyboard_insert_index(storyboards, payload)

    item = payload.model_dump(exclude_none=True)
    item.pop("insert_after_storyboard_id", None)
    item["shot_number"] = insert_index + 1
    storyboard = make_storyboard_from_item(
        project_id=project_id,
        chapter_id=chapter_id,
        user_id=user_id,
        ai_model_id=None,
        item=item,
        index=insert_index + 1,
        extra={"operation": "manual_create"},
    )
    db.add(storyboard)
    await db.flush()

    final_order = storyboards[:insert_index] + [storyboard] + storyboards[insert_index:]
    _assign_shot_numbers(final_order)
    await db.commit()
    await db.refresh(storyboard)
    return storyboard


async def update_project_storyboard(
    db: AsyncSession,
    project_id: UUID,
    chapter_id: UUID,
    storyboard_id: UUID,
    user_id: UUID,
    payload: ProjectStoryboardUpdateRequest,
) -> ProjectStoryboard:
    storyboard = await get_project_storyboard_or_404(
        db, project_id, chapter_id, storyboard_id, user_id
    )
    await _lock_storyboard_order(db, project_id, chapter_id, user_id)
    update_data = payload.model_dump(exclude_unset=True)
    requested_shot_number = update_data.pop("shot_number", None)
    event_goal = update_data.pop("event_goal", None)
    split_reason = update_data.pop("split_reason", None)
    changed = bool(update_data) or event_goal is not None or split_reason is not None
    for field, value in update_data.items():
        setattr(storyboard, field, value)
    if event_goal is not None or split_reason is not None:
        storyboard.extra = {
            **(storyboard.extra or {}),
            **({"event_goal": event_goal} if event_goal is not None else {}),
            **({"split_reason": split_reason} if split_reason is not None else {}),
        }
    now = beijing_datetime()
    if changed:
        extra = dict(storyboard.extra or {})
        for media_type in ("image", "video"):
            status_key = f"{media_type}_generation_status"
            if extra.get(status_key) in {"success", "selected"}:
                extra[status_key] = "invalidated"
        storyboard.extra = extra
        history_result = await db.execute(
            select(ProjectGeneratedAsset).where(
                ProjectGeneratedAsset.target_type == "storyboard",
                ProjectGeneratedAsset.target_id == storyboard.id,
                ProjectGeneratedAsset.media_type.in_(("image", "video")),
                ProjectGeneratedAsset.is_enabled.is_(True),
            )
        )
        for history in history_result.scalars().all():
            history.extra = {
                **(history.extra or {}),
                "validity_status": "invalidated",
                "invalidated_by": "storyboard_edit",
                "invalidated_at": now.isoformat(),
            }
    if requested_shot_number is not None:
        storyboards = await list_enabled_storyboards(db, project_id, chapter_id, user_id)
        remaining = [item for item in storyboards if item.id != storyboard.id]
        insert_index = min(max(requested_shot_number - 1, 0), len(remaining))
        _assign_shot_numbers(remaining[:insert_index] + [storyboard] + remaining[insert_index:])
    storyboard.updated_at = now
    await db.commit()
    await db.refresh(storyboard)
    return storyboard


async def delete_project_storyboard(
    db: AsyncSession,
    project_id: UUID,
    chapter_id: UUID,
    storyboard_id: UUID,
    user_id: UUID,
) -> ProjectStoryboard:
    storyboard = await get_project_storyboard_or_404(
        db, project_id, chapter_id, storyboard_id, user_id
    )
    await _lock_storyboard_order(db, project_id, chapter_id, user_id)
    await cancel_project_resource_task_records(
        db,
        project_id,
        user_id,
        match_extra={"storyboard_id": storyboard_id},
        reason="分镜已删除",
    )
    storyboard.is_enabled = False
    storyboard.updated_at = beijing_datetime()
    storyboards = await list_enabled_storyboards(db, project_id, chapter_id, user_id)
    _assign_shot_numbers([item for item in storyboards if item.id != storyboard.id])
    await db.commit()
    await db.refresh(storyboard)
    return storyboard


async def merge_project_storyboards(
    db: AsyncSession,
    project_id: UUID,
    chapter_id: UUID,
    user_id: UUID,
    payload: ProjectStoryboardMergeRequest,
) -> List[ProjectStoryboard]:
    await get_project_chapter_or_404(db, project_id, chapter_id, user_id)
    await _lock_storyboard_order(db, project_id, chapter_id, user_id)
    storyboard_ids = _unique_uuids(payload.storyboard_ids)
    if len(storyboard_ids) < 2:
        raise AppException("至少选择两个分镜进行合并", code=40031, status_code=400)

    storyboards = await list_enabled_storyboards(db, project_id, chapter_id, user_id)
    selected_ids = set(storyboard_ids)
    selected_map = {
        storyboard.id: storyboard for storyboard in storyboards if storyboard.id in selected_ids
    }
    if len(selected_map) != len(storyboard_ids):
        raise AppException("待合并分镜不存在或已不可用", code=40410, status_code=404)

    selected = [storyboard for storyboard in storyboards if storyboard.id in selected_map]
    first_selected_index = next(
        index for index, storyboard in enumerate(storyboards) if storyboard.id in selected_map
    )
    merged_item = _build_merged_storyboard_item(selected, payload)

    now = beijing_datetime()
    for storyboard in selected:
        storyboard.is_enabled = False
        storyboard.updated_at = now

    merged_storyboard = make_storyboard_from_item(
        project_id=project_id,
        chapter_id=chapter_id,
        user_id=user_id,
        ai_model_id=selected[0].ai_model_id,
        item=merged_item,
        index=first_selected_index + 1,
        extra={
            "operation": "merge",
            "merged_storyboard_ids": [str(storyboard.id) for storyboard in selected],
        },
    )
    db.add(merged_storyboard)
    await db.flush()

    final_order: List[ProjectStoryboard] = []
    merged_inserted = False
    for index, storyboard in enumerate(storyboards):
        if index == first_selected_index:
            final_order.append(merged_storyboard)
            merged_inserted = True
        if storyboard.id not in selected_map:
            final_order.append(storyboard)
    if not merged_inserted:
        final_order.append(merged_storyboard)

    _assign_shot_numbers(final_order)
    await db.commit()
    return await list_enabled_storyboards(db, project_id, chapter_id, user_id)


async def split_project_storyboard(
    db: AsyncSession,
    project_id: UUID,
    chapter_id: UUID,
    storyboard_id: UUID,
    user_id: UUID,
    payload: ProjectStoryboardSplitRequest,
) -> List[ProjectStoryboard]:
    storyboard = await get_project_storyboard_or_404(
        db, project_id, chapter_id, storyboard_id, user_id
    )
    await _lock_storyboard_order(db, project_id, chapter_id, user_id)
    storyboards = await list_enabled_storyboards(db, project_id, chapter_id, user_id)
    original_index = next(
        (index for index, item in enumerate(storyboards) if item.id == storyboard.id), None
    )
    if original_index is None:
        raise AppException("项目分镜不存在", code=40410, status_code=404)

    now = beijing_datetime()
    storyboard.is_enabled = False
    storyboard.updated_at = now

    new_storyboards: List[ProjectStoryboard] = []
    for offset, unit in enumerate(payload.units, start=1):
        item = normalize_storyboard_item(unit.model_dump(exclude_none=True), offset)
        new_storyboard = make_storyboard_from_item(
            project_id=project_id,
            chapter_id=chapter_id,
            user_id=user_id,
            ai_model_id=storyboard.ai_model_id,
            item=item,
            index=storyboard.shot_number + offset - 1,
            extra={
                "operation": "split",
                "split_from_storyboard_id": str(storyboard.id),
            },
        )
        db.add(new_storyboard)
        new_storyboards.append(new_storyboard)
    await db.flush()

    final_order = storyboards[:original_index] + new_storyboards + storyboards[original_index + 1 :]
    _assign_shot_numbers(final_order)
    await db.commit()
    return await list_enabled_storyboards(db, project_id, chapter_id, user_id)


def make_storyboard_from_item(
    *,
    project_id: UUID,
    chapter_id: UUID,
    user_id: UUID,
    ai_model_id: Optional[UUID],
    item: Dict[str, Any],
    index: int,
    extra: Optional[Dict[str, Any]] = None,
) -> ProjectStoryboard:
    event_goal = _optional_str(item.get("event_goal"))
    split_reason = _optional_str(item.get("split_reason"))
    item_extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    estimated_duration_seconds = _positive_duration_seconds(item.get("estimated_duration_seconds"))
    return ProjectStoryboard(
        project_id=project_id,
        chapter_id=chapter_id,
        user_id=user_id,
        ai_model_id=ai_model_id,
        shot_number=_as_int(item.get("shot_number"), index),
        title=str(item.get("title") or f"分镜{index}")[:128],
        source_content=str(item.get("source_content") or ""),
        scene_name=_optional_str(item.get("scene_name"), 128),
        scene_state=_optional_str(item.get("scene_state"), 128),
        shot_size=_optional_str(item.get("shot_size"), 64),
        camera_angle=_optional_str(item.get("camera_angle"), 128),
        camera_movement=_optional_str(item.get("camera_movement")),
        screen_execution=_optional_str(item.get("screen_execution")),
        characters=_as_string_list(item.get("characters")),
        props=_as_string_list(item.get("props")),
        action=_optional_str(item.get("action")),
        character_action=_optional_str(item.get("character_action")),
        character_expression=_optional_str(item.get("character_expression")),
        dialogue=_optional_str(item.get("dialogue")),
        sound_effect=_optional_str(item.get("sound_effect")),
        atmosphere=_optional_str(item.get("atmosphere")),
        image_prompt=_optional_str(item.get("image_prompt")),
        video_prompt=_optional_str(item.get("video_prompt")),
        duration_suggestion=_optional_str(item.get("duration_suggestion"), 64),
        production_focus=_optional_str(item.get("production_focus")),
        negative_prompt=_optional_str(item.get("negative_prompt")),
        ending_frame=_optional_str(item.get("ending_frame")),
        extra={
            **item_extra,
            **(extra or {}),
            **(
                {"estimated_duration_seconds": estimated_duration_seconds}
                if estimated_duration_seconds is not None
                else {}
            ),
            **({"event_goal": event_goal} if event_goal else {}),
            **({"split_reason": split_reason} if split_reason else {}),
            "raw_item": item,
        },
        is_enabled=True,
    )


async def list_enabled_storyboards(
    db: AsyncSession,
    project_id: UUID,
    chapter_id: UUID,
    user_id: UUID,
) -> List[ProjectStoryboard]:
    result = await db.execute(
        select(ProjectStoryboard)
        .where(
            ProjectStoryboard.project_id == project_id,
            ProjectStoryboard.chapter_id == chapter_id,
            ProjectStoryboard.user_id == user_id,
            ProjectStoryboard.is_enabled.is_(True),
        )
        .order_by(
            ProjectStoryboard.shot_number.asc(),
            ProjectStoryboard.created_at.asc(),
            ProjectStoryboard.id.asc(),
        )
    )
    return list(result.scalars().all())


async def _lock_storyboard_order(
    db: AsyncSession,
    project_id: UUID,
    chapter_id: UUID,
    user_id: UUID,
) -> None:
    result = await db.execute(
        select(ProjectChapter.id)
        .where(
            ProjectChapter.id == chapter_id,
            ProjectChapter.project_id == project_id,
            ProjectChapter.user_id == user_id,
            ProjectChapter.is_enabled.is_(True),
        )
        .with_for_update()
    )
    if result.scalar_one_or_none() is None:
        raise AppException("项目章节不存在", code=40408, status_code=404)


def _build_merged_storyboard_item(
    storyboards: List[ProjectStoryboard],
    payload: ProjectStoryboardMergeRequest,
) -> Dict[str, Any]:
    first = storyboards[0]
    source_content = payload.source_content or _join_texts(
        storyboard.source_content for storyboard in storyboards
    )
    action = payload.action or _join_texts(storyboard.action for storyboard in storyboards)
    split_reason = payload.split_reason or "用户判断所选分镜属于同一连续事件，合并为一个分镜。"
    return {
        "shot_number": first.shot_number,
        "title": payload.title or _merged_title(storyboards),
        "source_content": source_content,
        "event_goal": payload.event_goal
        or _join_texts(storyboard.event_goal for storyboard in storyboards)
        or "推进连续事件",
        "scene_name": payload.scene_name
        or _first_nonempty(storyboard.scene_name for storyboard in storyboards),
        "characters": payload.characters
        if payload.characters is not None
        else _merge_string_lists(storyboard.characters for storyboard in storyboards),
        "props": payload.props
        if payload.props is not None
        else _merge_string_lists(storyboard.props for storyboard in storyboards),
        "action": action,
        "dialogue": payload.dialogue
        if payload.dialogue is not None
        else _join_texts(storyboard.dialogue for storyboard in storyboards),
        "split_reason": split_reason,
        "extra": payload.extra or {},
    }


def _merged_title(storyboards: List[ProjectStoryboard]) -> str:
    if len(storyboards) == 2:
        return f"{storyboards[0].title}合并"[:128]
    return f"{storyboards[0].title}等合并"[:128]


def _assign_shot_numbers(storyboards: List[ProjectStoryboard]) -> None:
    now = beijing_datetime()
    for index, storyboard in enumerate(storyboards, start=1):
        storyboard.shot_number = index
        storyboard.updated_at = now


def _resolve_storyboard_insert_index(
    storyboards: List[ProjectStoryboard],
    payload: ProjectStoryboardCreateRequest,
) -> int:
    if payload.insert_after_storyboard_id is not None:
        for index, storyboard in enumerate(storyboards):
            if storyboard.id == payload.insert_after_storyboard_id:
                return index + 1
        raise AppException("插入位置分镜不存在或已不可用", code=40410, status_code=404)
    if payload.shot_number is None:
        return len(storyboards)
    return min(max(payload.shot_number - 1, 0), len(storyboards))


def _unique_uuids(values: List[UUID]) -> List[UUID]:
    seen: set[UUID] = set()
    unique_values: List[UUID] = []
    for value in values:
        if value not in seen:
            unique_values.append(value)
            seen.add(value)
    return unique_values


def _join_texts(values: Any) -> str:
    texts: List[str] = []
    seen: set[str] = set()
    for value in values:
        if value in (None, ""):
            continue
        text = str(value).strip()
        if text and text not in seen:
            texts.append(text)
            seen.add(text)
    return "\n".join(texts)


def _first_nonempty(values: Any) -> str:
    for value in values:
        if value not in (None, ""):
            return str(value)
    return ""


def _merge_string_lists(values: Any) -> List[str]:
    merged: List[str] = []
    seen: set[str] = set()
    for value in values:
        for item in _as_string_list(value):
            if item not in seen:
                merged.append(item)
                seen.add(item)
    return merged
