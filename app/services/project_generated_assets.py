from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException
from app.core.timezone import beijing_datetime
from app.models.project_asset import ProjectCharacter, ProjectProp, ProjectScene
from app.models.project_generated_asset import ProjectGeneratedAsset
from app.models.project_storyboard import ProjectStoryboard
from app.models.task_record import UserTaskRecord
from app.services.projects import get_project_or_404


PROJECT_GENERATED_ASSET_TARGETS = {
    "character": {"model": ProjectCharacter, "media_type": "image"},
    "scene": {"model": ProjectScene, "media_type": "image"},
    "prop": {"model": ProjectProp, "media_type": "image"},
    "storyboard": {"model": ProjectStoryboard, "media_type": ("image", "video")},
}


async def create_project_generated_asset_history(
    db: AsyncSession,
    *,
    task_record: UserTaskRecord,
    target_type: str,
    target_id: UUID,
    media_type: str,
    result_urls: List[str],
    result_url: Optional[str] = None,
    last_frame_url: Optional[str] = None,
    chapter_id: Optional[UUID] = None,
    generation_mode: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
    select_current: bool = True,
) -> ProjectGeneratedAsset:
    if task_record.business_id is None:
        raise AppException("项目任务缺少项目ID，无法记录生成历史", code=50053, status_code=500)
    urls = _dedupe_urls(result_urls)
    selected_url = result_url or (urls[0] if urls else None)
    history = ProjectGeneratedAsset(
        project_id=task_record.business_id,
        chapter_id=chapter_id,
        user_id=task_record.user_id,
        task_record_id=task_record.id,
        ai_model_id=task_record.ai_model_id,
        target_type=target_type,
        target_id=target_id,
        media_type=media_type,
        result_url=selected_url,
        result_urls=urls,
        last_frame_url=last_frame_url or None,
        prompt=task_record.prompt,
        generation_mode=generation_mode or (task_record.extra or {}).get("generation_mode"),
        status="success",
        is_selected=select_current,
        extra=extra or {},
    )
    if select_current:
        await _clear_selected_history(
            db,
            project_id=task_record.business_id,
            user_id=task_record.user_id,
            target_type=target_type,
            target_id=target_id,
            media_type=media_type,
        )
    db.add(history)
    await db.flush()
    return history


async def list_project_generated_asset_history(
    db: AsyncSession,
    *,
    project_id: UUID,
    user_id: UUID,
    target_type: str,
    target_id: UUID,
    media_type: Optional[str],
    page: int,
    page_size: int,
) -> Tuple[List[ProjectGeneratedAsset], int]:
    await get_project_or_404(db, project_id, user_id)
    resolved_media_type = await _ensure_target_access(db, project_id, user_id, target_type, target_id, media_type)
    conditions = [
        ProjectGeneratedAsset.project_id == project_id,
        ProjectGeneratedAsset.user_id == user_id,
        ProjectGeneratedAsset.target_type == target_type,
        ProjectGeneratedAsset.target_id == target_id,
        ProjectGeneratedAsset.media_type == resolved_media_type,
        ProjectGeneratedAsset.is_enabled.is_(True),
    ]
    count_result = await db.execute(select(func.count()).select_from(ProjectGeneratedAsset).where(*conditions))
    total = int(count_result.scalar_one())
    result = await db.execute(
        select(ProjectGeneratedAsset)
        .where(*conditions)
        .order_by(ProjectGeneratedAsset.created_at.desc(), ProjectGeneratedAsset.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(result.scalars().all()), total


async def select_project_generated_asset_history(
    db: AsyncSession,
    *,
    project_id: UUID,
    user_id: UUID,
    history_id: UUID,
    target_type: Optional[str] = None,
    target_id: Optional[UUID] = None,
    media_type: Optional[str] = None,
    result_url: Optional[str] = None,
) -> Tuple[ProjectGeneratedAsset, Optional[str], Optional[str]]:
    await get_project_or_404(db, project_id, user_id)
    history = await db.get(ProjectGeneratedAsset, history_id)
    if history is None or not history.is_enabled:
        raise AppException("生成历史不存在", code=40412, status_code=404)
    if history.project_id != project_id or history.user_id != user_id:
        raise AppException("生成历史不存在", code=40412, status_code=404)
    if target_type and history.target_type != target_type:
        raise AppException("生成历史与目标资产不匹配", code=40034, status_code=400)
    if target_id and history.target_id != target_id:
        raise AppException("生成历史与目标资产不匹配", code=40034, status_code=400)
    if media_type and history.media_type != media_type:
        raise AppException("生成历史媒体类型不匹配", code=40035, status_code=400)

    selected_url = _select_result_url(history, result_url)
    target = await _ensure_target_access(
        db,
        project_id,
        user_id,
        history.target_type,
        history.target_id,
        history.media_type,
        return_target=True,
    )
    await _clear_selected_history(
        db,
        project_id=project_id,
        user_id=user_id,
        target_type=history.target_type,
        target_id=history.target_id,
        media_type=history.media_type,
    )
    history.result_url = selected_url
    history.is_selected = True
    history.updated_at = beijing_datetime()
    _apply_selected_history_to_target(target, history, selected_url)
    await db.commit()
    await db.refresh(history)
    return history, selected_url, history.last_frame_url


async def _clear_selected_history(
    db: AsyncSession,
    *,
    project_id: UUID,
    user_id: UUID,
    target_type: str,
    target_id: UUID,
    media_type: str,
) -> None:
    await db.execute(
        update(ProjectGeneratedAsset)
        .where(
            ProjectGeneratedAsset.project_id == project_id,
            ProjectGeneratedAsset.user_id == user_id,
            ProjectGeneratedAsset.target_type == target_type,
            ProjectGeneratedAsset.target_id == target_id,
            ProjectGeneratedAsset.media_type == media_type,
            ProjectGeneratedAsset.is_selected.is_(True),
        )
        .values(is_selected=False)
    )


async def _ensure_target_access(
    db: AsyncSession,
    project_id: UUID,
    user_id: UUID,
    target_type: str,
    target_id: UUID,
    media_type: Optional[str],
    return_target: bool = False,
) -> Any:
    config = PROJECT_GENERATED_ASSET_TARGETS.get(target_type)
    if config is None:
        raise AppException("不支持的生成历史目标类型", code=40033, status_code=400)
    expected_media_types = _as_media_types(config["media_type"])
    if media_type and media_type not in expected_media_types:
        raise AppException("生成历史媒体类型不匹配", code=40035, status_code=400)
    if not media_type and len(expected_media_types) != 1:
        raise AppException("请选择生成历史媒体类型", code=40035, status_code=400)
    model = config["model"]
    result = await db.execute(
        select(model).where(
            model.id == target_id,
            model.project_id == project_id,
            model.user_id == user_id,
            model.is_enabled.is_(True),
        )
    )
    target = result.scalar_one_or_none()
    if target is None:
        raise AppException("目标资产不存在", code=40413, status_code=404)
    resolved_media_type = media_type or expected_media_types[0]
    return target if return_target else resolved_media_type


def _apply_selected_history_to_target(target: Any, history: ProjectGeneratedAsset, selected_url: Optional[str]) -> None:
    now = beijing_datetime()
    if history.media_type == "image":
        target.extra = {
            **(target.extra or {}),
            "image_generation_status": "selected",
            "image_generation_history_id": str(history.id),
            "image_generation_task_record_id": str(history.task_record_id) if history.task_record_id else "",
            "image_generation_result": selected_url or "",
        }
        if hasattr(target, "reference_image"):
            target.reference_image = selected_url
    elif history.media_type == "video":
        target.extra = {
            **(target.extra or {}),
            "video_generation_status": "selected",
            "video_generation_history_id": str(history.id),
            "video_generation_task_record_id": str(history.task_record_id) if history.task_record_id else "",
            "video_generation_result": selected_url or "",
            **({"video_generation_last_frame_url": history.last_frame_url} if history.last_frame_url else {}),
        }
    target.updated_at = now


def _select_result_url(history: ProjectGeneratedAsset, result_url: Optional[str]) -> Optional[str]:
    if not result_url:
        return history.result_url or (history.result_urls[0] if history.result_urls else None)
    if result_url not in (history.result_urls or []):
        raise AppException("选择的结果地址不属于该生成历史", code=40036, status_code=400)
    return result_url


def _as_media_types(value: Any) -> Tuple[str, ...]:
    if isinstance(value, tuple):
        return value
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    return (str(value),)


def _dedupe_urls(urls: List[str]) -> List[str]:
    result: List[str] = []
    for raw_url in urls or []:
        url = str(raw_url or "").strip()
        if url and url not in result:
            result.append(url)
    return result


def extract_result_urls(content: str) -> List[str]:
    return _dedupe_urls(
        [
            value.strip()
            for value in (content or "").split(",")
            if value.strip().startswith(("http://", "https://"))
        ]
    )
