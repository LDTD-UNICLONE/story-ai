"""Apply task outcomes to business records and generation history.

These functions never commit: task lifecycle callers own the surrounding transaction.
"""

from typing import Any, Dict, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import beijing_datetime
from app.models.agent_story_bible import AgentAssetVariant
from app.models.conversation import ConversationMessage
from app.models.project_asset import ProjectCharacter, ProjectProp, ProjectScene
from app.models.project_chapter import ProjectChapter
from app.models.project_storyboard import ProjectStoryboard
from app.models.task_record import UserTaskRecord
from app.services.agent.core_asset_changes import track_core_asset_reference_change
from app.services.generation.runner import ModelRunResult
from app.services.projects.generated_assets import (
    create_project_generated_asset_history,
    extract_result_urls,
    record_storyboard_video_generation_success,
)


async def sync_reconciled_task_success(
    db: AsyncSession, record: UserTaskRecord, model_result: ModelRunResult
) -> None:
    if (record.extra or {}).get("canvas_generation_id"):
        from app.services.projects.canvas_results import save_result
        await save_result(db, record, model_result)
    elif record.business_type == "conversation":
        await _sync_conversation_message_success(db, record, model_result)
    elif record.generation_type == "asset_image_generate":
        await _sync_asset_image_success(db, record, model_result)
    elif record.generation_type == "storyboard_image":
        await _sync_storyboard_image_success(db, record, model_result)
    elif record.generation_type == "storyboard_video":
        await _sync_storyboard_video_success(db, record, model_result)


async def sync_reconciled_task_failure(
    db: AsyncSession, record: UserTaskRecord, reason: str
) -> None:
    if record.business_type == "conversation":
        assistant_message_id = (record.extra or {}).get("assistant_message_id")
        parsed_assistant_message_id = _parse_uuid(assistant_message_id)
        if parsed_assistant_message_id:
            assistant_message = await db.get(ConversationMessage, parsed_assistant_message_id)
            if assistant_message:
                assistant_message.content = f"任务执行失败：{reason}"
                assistant_message.extra = {
                    **(assistant_message.extra or {}),
                    "task_status": "failed",
                    "failed_reason": reason,
                    "task_record_id": str(record.id),
                }
    elif record.generation_type == "asset_image_generate":
        await _sync_asset_image_failed(db, record, reason)
    elif record.generation_type == "storyboard_image":
        await _sync_storyboard_image_failed(db, record, reason)
    elif record.generation_type == "storyboard_video":
        storyboard_id = (record.extra or {}).get("storyboard_id")
        parsed_storyboard_id = _parse_uuid(storyboard_id)
        if parsed_storyboard_id:
            storyboard = await db.get(ProjectStoryboard, parsed_storyboard_id)
            if storyboard:
                storyboard.extra = {
                    **(storyboard.extra or {}),
                    "video_generation_status": "failed",
                    "video_generation_failed_reason": reason,
                    "video_generation_task_record_id": str(record.id),
                }


async def sync_task_business_failure(
    db: AsyncSession,
    record: UserTaskRecord,
    reason: str,
) -> None:
    if record.business_type == "conversation":
        await _sync_conversation_failed(db, record, reason)
        return
    if record.generation_type == "chapter_text_process":
        await _sync_chapter_text_failed(db, record, reason)
        return
    if record.generation_type in {
        "character_analysis",
        "scene_analysis",
        "prop_analysis",
        "storyboard_analysis",
        "storyboard_refinement",
        "storyboard_image_prompt",
        "storyboard_prompt_generation",
    }:
        await _sync_chapter_analysis_failed(db, record, reason)
        return
    if record.generation_type == "asset_image_generate":
        await _sync_asset_image_failed(db, record, reason)
        return
    if record.generation_type == "storyboard_image":
        await _sync_storyboard_image_failed(db, record, reason)
        return
    if record.generation_type == "storyboard_video":
        await _sync_storyboard_video_failed(db, record, reason)


async def _sync_conversation_failed(db: AsyncSession, record: UserTaskRecord, reason: str) -> None:
    assistant_message_id = (record.extra or {}).get("assistant_message_id")
    parsed_assistant_message_id = _parse_uuid(assistant_message_id)
    if parsed_assistant_message_id is None:
        return
    result = await db.execute(
        select(ConversationMessage).where(
            ConversationMessage.id == parsed_assistant_message_id,
            ConversationMessage.user_id == record.user_id,
            ConversationMessage.conversation_id == record.business_id,
            ConversationMessage.role == "assistant",
        )
    )
    assistant_message = result.scalar_one_or_none()
    if assistant_message is None:
        return
    assistant_message.content = f"任务执行失败：{reason}"
    assistant_message.extra = {
        **(assistant_message.extra or {}),
        "task_status": "failed",
        "failed_reason": reason,
        "task_record_id": str(record.id),
    }
    if assistant_message.message_type == "text":
        assistant_message.status = "failed"


async def _sync_chapter_text_failed(db: AsyncSession, record: UserTaskRecord, reason: str) -> None:
    chapter_id = (record.extra or {}).get("chapter_id")
    parsed_chapter_id = _parse_uuid(chapter_id)
    if parsed_chapter_id is None:
        return
    chapter = await db.get(ProjectChapter, parsed_chapter_id)
    if chapter is None:
        return
    chapter.process_status = "failed"
    chapter.extra = {
        **(chapter.extra or {}),
        "failed_reason": reason,
        "task_record_id": str(record.id),
    }


async def _sync_chapter_analysis_failed(
    db: AsyncSession, record: UserTaskRecord, reason: str
) -> None:
    chapter_id = (record.extra or {}).get("chapter_id")
    parsed_chapter_id = _parse_uuid(chapter_id)
    if parsed_chapter_id is None:
        return
    chapter = await db.get(ProjectChapter, parsed_chapter_id)
    if chapter is None:
        return
    status_key = {
        "character_analysis": "character_analysis_status",
        "scene_analysis": "scene_analysis_status",
        "prop_analysis": "prop_analysis_status",
        "storyboard_analysis": "storyboard_analysis_status",
        "storyboard_refinement": "storyboard_refinement_status",
        "storyboard_image_prompt": "storyboard_image_prompt_generation_status",
        "storyboard_prompt_generation": "storyboard_prompt_generation_status",
    }.get(record.generation_type)
    task_key = {
        "character_analysis": "character_analysis_task_record_id",
        "scene_analysis": "scene_analysis_task_record_id",
        "prop_analysis": "prop_analysis_task_record_id",
        "storyboard_analysis": "storyboard_analysis_task_record_id",
        "storyboard_refinement": "storyboard_refinement_task_record_id",
        "storyboard_image_prompt": "storyboard_image_prompt_generation_task_record_id",
        "storyboard_prompt_generation": "storyboard_prompt_generation_task_record_id",
    }.get(record.generation_type)
    if not status_key:
        return
    chapter.extra = {
        **(chapter.extra or {}),
        status_key: "failed",
        status_key.replace("_status", "_failed_reason"): reason,
        **({task_key: str(record.id)} if task_key else {}),
    }


async def _sync_asset_image_failed(db: AsyncSession, record: UserTaskRecord, reason: str) -> None:
    variant_id = _parse_uuid((record.extra or {}).get("agent_asset_variant_id"))
    if variant_id is not None:
        variant = await db.get(AgentAssetVariant, variant_id)
        if variant is not None:
            variant.extra = {
                **(variant.extra or {}),
                "image_generation_status": "failed",
                "image_generation_failed_reason": reason,
                "image_generation_task_record_id": str(record.id),
            }
            variant.updated_at = beijing_datetime()
        return
    asset_type = (record.extra or {}).get("asset_type")
    asset_id = (record.extra or {}).get("asset_id")
    model = {
        "character": ProjectCharacter,
        "scene": ProjectScene,
        "prop": ProjectProp,
    }.get(str(asset_type))
    parsed_asset_id = _parse_uuid(asset_id)
    if model is None or parsed_asset_id is None:
        return
    asset = await db.get(model, parsed_asset_id)
    if asset is None:
        return
    asset.extra = {
        **(asset.extra or {}),
        "image_generation_status": "failed",
        "image_generation_failed_reason": reason,
        "image_generation_task_record_id": str(record.id),
    }


async def _sync_storyboard_video_failed(
    db: AsyncSession, record: UserTaskRecord, reason: str
) -> None:
    storyboard_id = (record.extra or {}).get("storyboard_id")
    parsed_storyboard_id = _parse_uuid(storyboard_id)
    if parsed_storyboard_id is None:
        return
    storyboard = await db.get(ProjectStoryboard, parsed_storyboard_id)
    if storyboard is None:
        return
    storyboard.extra = {
        **(storyboard.extra or {}),
        "video_generation_status": "failed",
        "video_generation_failed_reason": reason,
        "video_generation_task_record_id": str(record.id),
    }


async def _sync_storyboard_image_failed(
    db: AsyncSession, record: UserTaskRecord, reason: str
) -> None:
    storyboard_id = (record.extra or {}).get("storyboard_id")
    parsed_storyboard_id = _parse_uuid(storyboard_id)
    if parsed_storyboard_id is None:
        return
    storyboard = await db.get(ProjectStoryboard, parsed_storyboard_id)
    if storyboard is None:
        return
    storyboard.extra = {
        **(storyboard.extra or {}),
        "image_generation_status": "failed",
        "image_generation_failed_reason": reason,
        "image_generation_task_record_id": str(record.id),
    }


async def _sync_conversation_message_success(
    db: AsyncSession,
    record: UserTaskRecord,
    model_result: ModelRunResult,
) -> None:
    assistant_message_id = (record.extra or {}).get("assistant_message_id")
    parsed_assistant_message_id = _parse_uuid(assistant_message_id)
    if parsed_assistant_message_id is None:
        return
    assistant_message = await db.get(ConversationMessage, parsed_assistant_message_id)
    if assistant_message is None:
        return
    assistant_message.content = model_result.content
    assistant_message.extra = {
        **(assistant_message.extra or {}),
        **model_result.extra,
        "task_status": "success",
        "task_record_id": str(record.id),
    }
    record.extra = {
        **(record.extra or {}),
        "assistant_message_extra": model_result.extra,
        "assistant_message_id": str(assistant_message.id),
    }


async def _sync_asset_image_success(
    db: AsyncSession,
    record: UserTaskRecord,
    model_result: ModelRunResult,
) -> None:
    variant_id = _parse_uuid((record.extra or {}).get("agent_asset_variant_id"))
    if variant_id is not None:
        variant = await db.get(AgentAssetVariant, variant_id)
        image_url = _first_result_url(model_result.content)
        if variant is None or not image_url:
            return
        asset_id = _parse_uuid((record.extra or {}).get("asset_id"))
        asset_type = str((record.extra or {}).get("asset_type") or "")
        if (
            record.business_id is not None
            and asset_id is not None
            and asset_type in {"character", "scene", "prop"}
        ):
            await track_core_asset_reference_change(
                db,
                project_id=record.business_id,
                user_id=record.user_id,
                asset_type=asset_type,
                asset_id=asset_id,
                variant_id=variant.id,
                previous_reference_image=variant.reference_image,
                new_reference_image=image_url,
                source="worker",
            )
        history = await create_project_generated_asset_history(
            db,
            task_record=record,
            target_type="asset_variant",
            target_id=variant.id,
            media_type="image",
            result_urls=extract_result_urls(model_result.content) or [image_url],
            result_url=image_url,
            generation_mode=(record.extra or {}).get("generation_mode"),
            extra={
                "variant_name": variant.canonical_name,
                "generation_ratio": (record.extra or {}).get("generation_ratio"),
                "model_result_extra": model_result.extra,
            },
        )
        variant.reference_image = image_url
        variant.lock_version += 1
        variant.updated_at = beijing_datetime()
        variant.extra = {
            **(variant.extra or {}),
            "image_generation_status": "success",
            "image_generation_history_id": str(history.id),
            "image_generation_task_record_id": str(record.id),
            "image_generation_extra": model_result.extra,
        }
        record.result = image_url
        record.extra = {
            **(record.extra or {}),
            "oss_image_url": image_url,
            "generated_asset_history_id": str(history.id),
        }
        return
    asset_type = (record.extra or {}).get("asset_type")
    asset_id = (record.extra or {}).get("asset_id")
    model = {
        "character": ProjectCharacter,
        "scene": ProjectScene,
        "prop": ProjectProp,
    }.get(str(asset_type))
    if model is None or not asset_id:
        return
    parsed_asset_id = _parse_uuid(asset_id)
    if parsed_asset_id is None:
        return
    asset = await db.get(model, parsed_asset_id)
    if asset is None:
        return
    image_url = _first_result_url(model_result.content)
    if not image_url:
        return
    history = await create_project_generated_asset_history(
        db,
        task_record=record,
        target_type=str(asset_type),
        target_id=parsed_asset_id,
        media_type="image",
        result_urls=extract_result_urls(model_result.content) or [image_url],
        result_url=image_url,
        generation_mode=(record.extra or {}).get("generation_mode"),
        extra={
            "asset_name": (record.extra or {}).get("asset_name"),
            "generation_ratio": (record.extra or {}).get("generation_ratio"),
            "model_result_extra": model_result.extra,
        },
    )
    await track_core_asset_reference_change(
        db,
        project_id=asset.project_id,
        user_id=asset.user_id,
        asset_type=str(asset_type),
        asset_id=asset.id,
        previous_reference_image=asset.reference_image,
        new_reference_image=image_url,
        source="worker",
    )
    asset.reference_image = image_url
    asset.updated_at = beijing_datetime()
    asset.extra = {
        **(asset.extra or {}),
        "image_generation_status": "success",
        "image_generation_history_id": str(history.id),
        "image_generation_task_record_id": str(record.id),
        "image_generation_extra": model_result.extra,
    }
    record.result = image_url
    record.extra = {
        **(record.extra or {}),
        "oss_image_url": image_url,
        "generated_asset_history_id": str(history.id),
    }


async def _sync_storyboard_image_success(
    db: AsyncSession,
    record: UserTaskRecord,
    model_result: ModelRunResult,
) -> None:
    storyboard_id = (record.extra or {}).get("storyboard_id")
    parsed_storyboard_id = _parse_uuid(storyboard_id)
    if parsed_storyboard_id is None:
        return
    storyboard = await db.get(ProjectStoryboard, parsed_storyboard_id)
    if storyboard is None:
        return
    image_url = _first_result_url(model_result.content)
    if not image_url:
        return
    result_urls = extract_result_urls(model_result.content) or [image_url]
    history = await create_project_generated_asset_history(
        db,
        task_record=record,
        target_type="storyboard",
        target_id=parsed_storyboard_id,
        media_type="image",
        result_urls=result_urls,
        result_url=image_url,
        chapter_id=storyboard.chapter_id,
        generation_mode="storyboard_image",
        extra={
            "storyboard_title": storyboard.title,
            "shot_number": storyboard.shot_number,
            "aspect_ratio": (record.extra or {}).get("aspect_ratio"),
            "reference_images": (record.extra or {}).get("reference_images"),
            "model_result_extra": model_result.extra,
        },
    )
    storyboard.extra = {
        **(storyboard.extra or {}),
        "image_generation_status": "success",
        "image_generation_history_id": str(history.id),
        "image_generation_task_record_id": str(record.id),
        "image_generation_result": image_url,
        "image_generation_result_urls": result_urls,
        "image_generation_extra": model_result.extra,
    }
    storyboard.updated_at = beijing_datetime()
    record.result = image_url
    record.extra = {
        **(record.extra or {}),
        "oss_image_url": image_url,
        "storyboard_image_result": image_url,
        "generated_asset_history_id": str(history.id),
    }


async def _sync_storyboard_video_success(
    db: AsyncSession,
    record: UserTaskRecord,
    model_result: ModelRunResult,
) -> None:
    storyboard_id = (record.extra or {}).get("storyboard_id")
    parsed_storyboard_id = _parse_uuid(storyboard_id)
    if parsed_storyboard_id is None:
        return
    storyboard = await db.get(ProjectStoryboard, parsed_storyboard_id)
    if storyboard is None:
        return
    last_frame_url = _first_generated_last_frame_url(model_result.extra)
    await record_storyboard_video_generation_success(
        db,
        task_record=record,
        storyboard=storyboard,
        content=model_result.content,
        result_extra=model_result.extra,
        last_frame_url=last_frame_url,
    )


def _first_result_url(content: str) -> str:
    for value in (content or "").split(","):
        url = value.strip()
        if url.startswith(("http://", "https://")):
            return url
    return ""


def _first_generated_last_frame_url(extra: Dict[str, Any]) -> str:
    for key in ("display_last_frame_urls", "oss_last_frame_urls"):
        value = extra.get(key)
        if isinstance(value, list):
            for item in value:
                if item:
                    return str(item)
        if value:
            return str(value)
    return ""


def _parse_uuid(value: Any) -> Optional[UUID]:
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None
