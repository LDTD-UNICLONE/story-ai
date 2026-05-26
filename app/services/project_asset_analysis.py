import json
import re
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple, Type
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException
from app.models.ai_model import AiModel
from app.models.project_asset import ProjectCharacter, ProjectProp, ProjectScene
from app.models.project_chapter import ProjectChapter
from app.models.task_record import UserTaskRecord
from app.models.user import User
from app.schemas.project_asset import ProjectAssetAnalyzeRequest
from app.services.model_points import calculate_text_submission_points_cost, settle_text_task_points
from app.services.model_runner import run_model
from app.services.points import change_user_points, consume_user_points
from app.services.project_chapter_processing import get_enabled_text_model_or_404
from app.services.project_chapters import get_project_chapter_or_404
from app.services.prompts import render_system_prompt
from app.services.projects import get_project_or_404
from app.services.task_records import create_user_task_record, refresh_task_record_interrupted
from app.services.text_model_extra import normalize_text_analysis_extra


ASSET_CONFIG: Dict[str, Dict[str, Any]] = {
    "character": {
        "model": ProjectCharacter,
        "prompt_file": "character_analysis.md",
        "generation_type": "character_analysis",
        "status_key": "character_analysis_status",
        "task_key": "character_analysis_task_record_id",
        "title": "人物分析",
    },
    "scene": {
        "model": ProjectScene,
        "prompt_file": "scene_analysis.md",
        "generation_type": "scene_analysis",
        "status_key": "scene_analysis_status",
        "task_key": "scene_analysis_task_record_id",
        "title": "场景分析",
    },
    "prop": {
        "model": ProjectProp,
        "prompt_file": "prop_analysis.md",
        "generation_type": "prop_analysis",
        "status_key": "prop_analysis_status",
        "task_key": "prop_analysis_task_record_id",
        "title": "道具分析",
    },
}


async def submit_asset_analysis(
    db: AsyncSession,
    project_id: UUID,
    chapter_id: UUID,
    asset_type: str,
    user: User,
    payload: ProjectAssetAnalyzeRequest,
) -> Tuple[UserTaskRecord, int]:
    config = _asset_config(asset_type)
    await get_project_or_404(db, project_id, user.id)
    chapter = await get_project_chapter_or_404(db, project_id, chapter_id, user.id)
    if not chapter.processed_content:
        raise AppException("章节还没有处理后的内容，无法分析资源", code=40011, status_code=400)

    ai_model = await get_enabled_text_model_or_404(db, payload.ai_model_id)
    points_cost = calculate_text_submission_points_cost(ai_model)
    points_transaction = None
    if points_cost > 0:
        points_transaction = await consume_user_points(
            db,
            user_id=user.id,
            amount=points_cost,
            remark=f"项目章节{config['title']}：{chapter.title}",
            auto_commit=False,
        )

    model_extra = normalize_text_analysis_extra(payload.extra)
    prompt_source = "custom" if payload.analysis_prompt else "system"
    prompt = payload.analysis_prompt or render_system_prompt(
        config["prompt_file"],
        processed_content=chapter.processed_content,
    )
    task_prompt = (
        _build_custom_prompt(prompt, chapter.processed_content)
        if prompt_source == "custom"
        else "系统提示词"
    )
    task_record = await create_user_task_record(
        db,
        user_id=user.id,
        ai_model_id=ai_model.id,
        points_transaction_id=points_transaction.id if points_transaction else None,
        business_type="project",
        business_id=project_id,
        generation_type=config["generation_type"],
        status="pending",
        title=f"{config['title']}：{chapter.title}",
        prompt=task_prompt,
        result=None,
        points_cost=points_cost,
        extra={
            "project_id": str(project_id),
            "chapter_id": str(chapter_id),
            "chapter_title": chapter.title,
            "asset_type": asset_type,
            "prompt_source": prompt_source,
            "model_extra": model_extra,
        },
    )
    if prompt_source == "custom":
        task_record.extra = {**(task_record.extra or {}), "analysis_prompt": prompt}
    chapter.extra = {
        **(chapter.extra or {}),
        config["status_key"]: "pending",
        config["task_key"]: str(task_record.id),
    }
    await db.commit()

    try:
        from app.tasks.project_asset_analysis import run_project_asset_analysis

        run_project_asset_analysis.delay(str(task_record.id), str(chapter_id), asset_type)
    except Exception:
        await _mark_enqueue_failed(db, task_record, chapter, config)
    return task_record, points_cost


async def run_asset_analysis_in_worker(
    db: AsyncSession,
    task_record: UserTaskRecord,
    chapter: ProjectChapter,
    asset_type: str,
) -> None:
    config = _asset_config(asset_type)
    result = await db.execute(
        select(AiModel).where(
            AiModel.id == task_record.ai_model_id,
            AiModel.model_type == "text",
            AiModel.is_enabled.is_(True),
        )
    )
    ai_model = result.scalar_one_or_none()
    if ai_model is None:
        raise AppException("文本模型不存在或已禁用", code=40404, status_code=404)

    model_snapshot = SimpleNamespace(
        id=ai_model.id,
        model_id=ai_model.model_id,
        vendor=ai_model.vendor,
        nickname=ai_model.nickname,
        points_cost=ai_model.points_cost,
        capabilities=ai_model.capabilities or {},
    )
    model_prompt = _resolve_model_prompt(task_record, chapter, config)
    model_result = await run_model(
        model_snapshot,
        "text",
        model_prompt,
        (task_record.extra or {}).get("model_extra") or {},
    )
    if await refresh_task_record_interrupted(db, task_record):
        return

    items = parse_asset_items(model_result.content)
    if not items:
        raise AppException("资源分析未返回有效数据", code=50231, status_code=502)

    await settle_text_task_points(
        db,
        task_record,
        ai_model,
        model_result.extra,
        remark_prefix=str(config["title"]),
    )

    model = config["model"]
    for item in items:
        asset = _build_asset(model, item, task_record, chapter)
        db.add(asset)

    chapter.extra = {
        **(chapter.extra or {}),
        config["status_key"]: "success",
        config["task_key"]: str(task_record.id),
    }
    task_record.status = "success"
    task_record.result = model_result.content
    task_record.extra = {
        **(task_record.extra or {}),
        "model_result_extra": model_result.extra,
        "asset_count": len(items),
    }


def parse_asset_items(content: str) -> List[Dict[str, Any]]:
    payload = _parse_json_object(content)
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _build_asset(model: Type[Any], item: Dict[str, Any], task_record: UserTaskRecord, chapter: ProjectChapter) -> Any:
    common = {
        "project_id": task_record.business_id,
        "user_id": task_record.user_id,
        "source_chapter_id": chapter.id,
        "name": _required_name(item),
        "description": _optional_str(item.get("description")),
        "prompt": _optional_str(item.get("prompt")),
        "reference_image": _optional_str(item.get("reference_image"), 512),
        "source_content": _optional_str(item.get("source_content")),
        "extra": _build_asset_extra(item, task_record),
        "is_enabled": True,
    }
    if model is ProjectCharacter:
        return ProjectCharacter(
            **common,
            aliases=_as_string_list(item.get("aliases")),
            identity=_optional_str(item.get("identity"), 128),
            gender=_optional_str(item.get("gender"), 32),
            age=_optional_str(item.get("age"), 64),
            appearance=_optional_str(item.get("appearance")),
            personality=_optional_str(item.get("personality")),
            relationship=_optional_str(item.get("relationship")),
            costume=_optional_str(item.get("costume")),
        )
    if model is ProjectScene:
        return ProjectScene(
            **common,
            location=_optional_str(item.get("location"), 255),
            time_of_day=_optional_str(item.get("time_of_day"), 64),
            environment=_optional_str(item.get("environment")),
            atmosphere=_optional_str(item.get("atmosphere")),
        )
    return ProjectProp(
        **common,
        category=_optional_str(item.get("category"), 64),
        appearance=_optional_str(item.get("appearance")),
        function=_optional_str(item.get("function")),
    )


def _resolve_model_prompt(task_record: UserTaskRecord, chapter: ProjectChapter, config: Dict[str, Any]) -> str:
    if (task_record.extra or {}).get("prompt_source") == "system":
        return render_system_prompt(config["prompt_file"], processed_content=chapter.processed_content or "")
    return task_record.prompt


def _build_asset_extra(item: Dict[str, Any], task_record: UserTaskRecord) -> Dict[str, Any]:
    item_extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    return {
        **item_extra,
        "task_record_id": str(task_record.id),
        "raw_item": item,
    }


def _build_custom_prompt(analysis_prompt: str, processed_content: str) -> str:
    return f"{analysis_prompt.strip()}\n\nprocessed_content：\n{processed_content.strip()}"


def _asset_config(asset_type: str) -> Dict[str, Any]:
    config = ASSET_CONFIG.get(asset_type)
    if not config:
        raise AppException("不支持的资源分析类型", code=40012, status_code=400)
    return config


def _parse_json_object(content: str) -> Dict[str, Any]:
    try:
        payload = json.loads(content)
        return payload if isinstance(payload, dict) else {}
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content or "", re.S)
        if not match:
            return {}
        try:
            payload = json.loads(match.group(0))
            return payload if isinstance(payload, dict) else {}
        except json.JSONDecodeError:
            return {}


def _required_name(item: Dict[str, Any]) -> str:
    name = str(item.get("name") or "").strip()
    return name[:128] or "未命名资源"


def _as_string_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item not in (None, "")]


def _optional_str(value: Any, max_length: int = 0) -> Any:
    if value in (None, ""):
        return None
    text = str(value)
    return text[:max_length] if max_length else text


async def _mark_enqueue_failed(
    db: AsyncSession,
    task_record: UserTaskRecord,
    chapter: ProjectChapter,
    config: Dict[str, Any],
) -> None:
    refund_transaction_id = None
    if task_record.points_cost > 0:
        refund_transaction = await change_user_points(
            db,
            user_id=task_record.user_id,
            amount=task_record.points_cost,
            transaction_type="refund",
            remark=f"任务入队失败退回积分：{task_record.title}",
            auto_commit=False,
        )
        refund_transaction_id = str(refund_transaction.id)
    task_record.status = "failed"
    task_record.result = "任务入队失败"
    task_record.extra = {
        **(task_record.extra or {}),
        "failed_reason": "任务入队失败",
        "refund_transaction_id": refund_transaction_id,
    }
    chapter.extra = {
        **(chapter.extra or {}),
        config["status_key"]: "failed",
        f"{config['status_key']}_failed_reason": "任务入队失败",
    }
    await db.commit()
