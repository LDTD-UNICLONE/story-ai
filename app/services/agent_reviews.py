import asyncio
import hashlib
import json
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from uuid import UUID, uuid4

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.exceptions import AppException
from app.core.outbound_url import open_safe_http_response, trusted_oss_hosts
from app.core.public_messages import sanitize_public_message
from app.core.timezone import beijing_datetime
from app.integrations.oss import OssClient
from app.models.agent_production import AgentEvent, AgentProduction
from app.models.agent_review import AgentDelivery, AgentEpisodeReview, AgentReviewIssue
from app.models.project import Project
from app.models.project_chapter import ProjectChapter
from app.models.project_generated_asset import ProjectGeneratedAsset
from app.models.project_storyboard import ProjectStoryboard
from app.models.user import User
from app.schemas.agent_review import (
    AgentDeliveryCreateRequest,
    AgentEpisodeApproveRequest,
    AgentJianyingExportCreateRequest,
    AgentReviewIssueCreateRequest,
    AgentReviewIssueUpdateRequest,
)
from app.services.agent_storyboard_bindings import storyboard_estimated_duration_seconds
from app.services.agent_storyboard_episode_state import (
    effective_storyboard_episode_status,
)
from app.services.agent_productions import get_agent_production_or_404
from app.services.agent_production_state import transition_production_status


SUCCESS_MEDIA_STATUSES = {"success", "selected"}


async def get_agent_production_review(
    db: AsyncSession,
    production_id: UUID,
    user_id: UUID,
) -> Dict[str, Any]:
    production, chapters, storyboards = await _review_scope(db, production_id, user_id)
    chapter_ids = [chapter.id for chapter in chapters]
    storyboard_ids = [storyboard.id for storyboard in storyboards]
    issues = await _issues(db, production_id)
    reviews = await _episode_reviews(db, production_id)
    version_counts = await _version_counts(db, production.project_id, storyboard_ids)
    issues_by_chapter = _group_issues(issues, "chapter_id")
    issues_by_storyboard = _group_issues(issues, "storyboard_id")
    review_by_chapter = {item.chapter_id: item for item in reviews}
    storyboards_by_chapter: Dict[UUID, List[ProjectStoryboard]] = {
        chapter_id: [] for chapter_id in chapter_ids
    }
    for storyboard in storyboards:
        storyboards_by_chapter.setdefault(storyboard.chapter_id, []).append(storyboard)

    episodes = []
    approved_count = 0
    for chapter in chapters:
        chapter_storyboards = storyboards_by_chapter.get(chapter.id, [])
        storyboard_analysis_status = effective_storyboard_episode_status(
            chapter,
            len(chapter_storyboards),
        )
        review = review_by_chapter.get(chapter.id)
        snapshot_hash = _media_snapshot_hash(chapter_storyboards)
        review_status = _effective_review_status(review, snapshot_hash)
        if review_status == "approved":
            approved_count += 1
        chapter_issues = issues_by_chapter.get(chapter.id, [])
        episodes.append(
            {
                "chapter_id": chapter.id,
                "episode_number": int((chapter.extra or {}).get("episode_number") or 0),
                "title": chapter.title,
                "storyboard_analysis_status": storyboard_analysis_status,
                "can_review": storyboard_analysis_status == "ready",
                "review_status": review_status,
                "review_lock_version": review.lock_version if review else 0,
                "approved_at": review.approved_at if review_status == "approved" else None,
                "issue_count": len(chapter_issues),
                "blocking_issue_count": _blocking_count(chapter_issues),
                "storyboards": [
                    _storyboard_review_item(
                        storyboard,
                        issues_by_storyboard.get(storyboard.id, []),
                        version_counts,
                        production,
                    )
                    for storyboard in chapter_storyboards
                ],
            }
        )
    open_issues = [issue for issue in issues if issue.status == "open"]
    blocking_count = _blocking_count(open_issues)
    return {
        "production_id": production.id,
        "status": production.status,
        "total_episode_count": len(episodes),
        "approved_episode_count": approved_count,
        "open_issue_count": len(open_issues),
        "blocking_issue_count": blocking_count,
        "delivery_ready": bool(episodes)
        and approved_count == len(episodes)
        and blocking_count == 0
        and not _missing_video_ids(storyboards),
        "episodes": episodes,
    }


async def get_agent_episode_video_timeline(
    db: AsyncSession,
    production_id: UUID,
    chapter_id: UUID,
    user_id: UUID,
) -> Dict[str, Any]:
    production, chapters, storyboards = await _review_scope(db, production_id, user_id)
    chapter = next((item for item in chapters if item.id == chapter_id), None)
    if chapter is None:
        raise AppException("审片剧集不存在", code=40440, status_code=404)
    chapter_storyboards = [item for item in storyboards if item.chapter_id == chapter.id]
    _require_reviewable_chapter(chapter, chapter_storyboards)
    review = next(
        (item for item in await _episode_reviews(db, production.id) if item.chapter_id == chapter.id),
        None,
    )
    blocking_result = await db.execute(
        select(func.count())
        .select_from(AgentReviewIssue)
        .where(
            AgentReviewIssue.production_id == production.id,
            AgentReviewIssue.chapter_id == chapter.id,
            AgentReviewIssue.status == "open",
            AgentReviewIssue.severity == "blocking",
        )
    )
    items = [
        {
            "storyboard_id": storyboard.id,
            "group_number": storyboard.shot_number,
            "title": storyboard.title,
            "estimated_duration_seconds": storyboard_estimated_duration_seconds(storyboard),
            "video_status": str(
                (storyboard.extra or {}).get("video_generation_status") or "not_started"
            ),
            "video_url": _media_url(storyboard, "video"),
            "video_history_id": _optional_uuid(
                (storyboard.extra or {}).get("video_generation_history_id")
            ),
        }
        for storyboard in chapter_storyboards
    ]
    snapshot_hash = _media_snapshot_hash(chapter_storyboards)
    return {
        "production_id": production.id,
        "chapter_id": chapter.id,
        "episode_number": int((chapter.extra or {}).get("episode_number") or 0),
        "title": chapter.title,
        "review_status": _effective_review_status(review, snapshot_hash),
        "review_lock_version": review.lock_version if review else 0,
        "ready_to_approve": bool(items)
        and not _missing_video_ids(chapter_storyboards)
        and int(blocking_result.scalar_one() or 0) == 0,
        "estimated_duration_seconds": sum(
            item["estimated_duration_seconds"] for item in items
        ),
        "items": items,
    }


async def create_agent_review_issue(
    db: AsyncSession,
    production_id: UUID,
    user: User,
    payload: AgentReviewIssueCreateRequest,
) -> AgentReviewIssue:
    production, chapters, storyboards = await _review_scope(db, production_id, user.id)
    chapter = next((item for item in chapters if item.id == payload.chapter_id), None)
    if chapter is None:
        raise AppException("审片剧集不存在", code=40440, status_code=404)
    chapter_storyboards = [item for item in storyboards if item.chapter_id == chapter.id]
    _require_reviewable_chapter(chapter, chapter_storyboards)
    if payload.storyboard_id is not None and not any(
        item.id == payload.storyboard_id and item.chapter_id == chapter.id for item in storyboards
    ):
        raise AppException("审片分镜不存在", code=40441, status_code=404)
    await _lock_review_scope(db, production, [chapter], [])
    issue = AgentReviewIssue(
        production_id=production.id,
        chapter_id=chapter.id,
        storyboard_id=payload.storyboard_id,
        user_id=user.id,
        media_type=payload.media_type,
        category=payload.category,
        severity=payload.severity,
        status="open",
        description=payload.description.strip(),
        lock_version=0,
        extra={},
    )
    db.add(issue)
    await db.flush()
    db.add(
        AgentEvent(
            production_id=production.id,
            actor_user_id=user.id,
            event_type="review.issue_created",
            source="user",
            payload={"issue_id": str(issue.id), "severity": issue.severity},
        )
    )
    await db.commit()
    await db.refresh(issue)
    return issue


async def update_agent_review_issue(
    db: AsyncSession,
    production_id: UUID,
    issue_id: UUID,
    user: User,
    payload: AgentReviewIssueUpdateRequest,
) -> AgentReviewIssue:
    await get_agent_production_or_404(db, production_id, user.id)
    result = await db.execute(
        select(AgentReviewIssue)
        .where(
            AgentReviewIssue.id == issue_id,
            AgentReviewIssue.production_id == production_id,
            AgentReviewIssue.user_id == user.id,
        )
        .with_for_update()
    )
    issue = result.scalar_one_or_none()
    if issue is None:
        raise AppException("审片问题不存在", code=40442, status_code=404)
    if issue.lock_version != payload.expected_lock_version:
        raise AppException(
            "审片问题版本冲突",
            code=40970,
            status_code=409,
            data={"current_lock_version": issue.lock_version},
        )
    if issue.status in {"resolved", "dismissed"}:
        return issue
    issue.status = payload.status
    issue.resolution_note = payload.resolution_note.strip()
    issue.resolved_by = user.id
    issue.resolved_at = beijing_datetime()
    issue.lock_version += 1
    db.add(
        AgentEvent(
            production_id=production_id,
            actor_user_id=user.id,
            event_type=f"review.issue_{payload.status}",
            source="user",
            payload={"issue_id": str(issue.id)},
        )
    )
    await db.commit()
    await db.refresh(issue)
    return issue


async def approve_agent_episode(
    db: AsyncSession,
    production_id: UUID,
    chapter_id: UUID,
    user: User,
    payload: AgentEpisodeApproveRequest,
) -> AgentEpisodeReview:
    production, chapters, storyboards = await _review_scope(db, production_id, user.id)
    chapter = next((item for item in chapters if item.id == chapter_id), None)
    if chapter is None:
        raise AppException("审片剧集不存在", code=40440, status_code=404)
    chapter_storyboards = [item for item in storyboards if item.chapter_id == chapter.id]
    _require_reviewable_chapter(chapter, chapter_storyboards)
    await _lock_review_scope(db, production, [chapter], chapter_storyboards)
    for storyboard in chapter_storyboards:
        await db.refresh(storyboard)
    missing = _missing_video_ids(chapter_storyboards)
    if not chapter_storyboards or missing:
        raise AppException(
            "剧集仍有未就绪视频",
            code=40972,
            status_code=409,
            data={"storyboard_ids": [str(value) for value in missing]},
        )
    blocking_result = await db.execute(
        select(func.count())
        .select_from(AgentReviewIssue)
        .where(
            AgentReviewIssue.production_id == production.id,
            AgentReviewIssue.chapter_id == chapter.id,
            AgentReviewIssue.status == "open",
            AgentReviewIssue.severity == "blocking",
        )
    )
    if int(blocking_result.scalar_one() or 0):
        raise AppException("剧集仍有未解决的阻断问题", code=40973, status_code=409)
    result = await db.execute(
        select(AgentEpisodeReview)
        .where(
            AgentEpisodeReview.production_id == production.id,
            AgentEpisodeReview.chapter_id == chapter.id,
        )
        .with_for_update()
    )
    review = result.scalar_one_or_none()
    current_version = review.lock_version if review else 0
    snapshot_hash = _media_snapshot_hash(chapter_storyboards)
    if review is not None and review.idempotency_key == payload.idempotency_key:
        if review.media_snapshot_hash == snapshot_hash:
            return review
        raise AppException("确认请求对应的媒体版本已经变化", code=40974, status_code=409)
    if payload.expected_lock_version != current_version:
        raise AppException(
            "剧集审片版本冲突",
            code=40975,
            status_code=409,
            data={"current_lock_version": current_version},
        )
    now = beijing_datetime()
    if review is None:
        review = AgentEpisodeReview(
            production_id=production.id,
            chapter_id=chapter.id,
            user_id=user.id,
            status="approved",
            media_snapshot_hash=snapshot_hash,
            approved_by=user.id,
            approved_at=now,
            lock_version=1,
            idempotency_key=payload.idempotency_key,
            extra={},
        )
        db.add(review)
    else:
        review.status = "approved"
        review.media_snapshot_hash = snapshot_hash
        review.approved_by = user.id
        review.approved_at = now
        review.lock_version += 1
        review.idempotency_key = payload.idempotency_key
    await db.flush()
    db.add(
        AgentEvent(
            production_id=production.id,
            actor_user_id=user.id,
            event_type="review.episode_approved",
            source="user",
            payload={
                "chapter_id": str(chapter.id),
                "media_snapshot_hash": snapshot_hash,
                "idempotency_key": payload.idempotency_key,
            },
        )
    )
    await db.commit()
    await db.refresh(review)
    return review


async def get_agent_delivery_readiness(
    db: AsyncSession,
    production_id: UUID,
    user_id: UUID,
) -> Dict[str, Any]:
    production, chapters, storyboards = await _review_scope(db, production_id, user_id)
    reviews = {item.chapter_id: item for item in await _episode_reviews(db, production.id)}
    storyboards_by_chapter = {
        chapter.id: [item for item in storyboards if item.chapter_id == chapter.id]
        for chapter in chapters
    }
    unapproved = []
    for chapter in chapters:
        review = reviews.get(chapter.id)
        if _effective_review_status(
            review,
            _media_snapshot_hash(storyboards_by_chapter[chapter.id]),
        ) != "approved":
            unapproved.append(chapter.id)
    blocking_result = await db.execute(
        select(func.count())
        .select_from(AgentReviewIssue)
        .where(
            AgentReviewIssue.production_id == production.id,
            AgentReviewIssue.status == "open",
            AgentReviewIssue.severity == "blocking",
        )
    )
    blocking_count = int(blocking_result.scalar_one() or 0)
    missing = _missing_video_ids(storyboards)
    return {
        "production_id": production.id,
        "ready": bool(chapters) and not unapproved and not blocking_count and not missing,
        "total_episode_count": len(chapters),
        "approved_episode_count": len(chapters) - len(unapproved),
        "unapproved_chapter_ids": unapproved,
        "open_blocking_issue_count": blocking_count,
        "missing_video_storyboard_ids": missing,
    }


async def create_agent_delivery(
    db: AsyncSession,
    production_id: UUID,
    user: User,
    payload: AgentDeliveryCreateRequest,
) -> AgentDelivery:
    production, chapters, storyboards = await _review_scope(db, production_id, user.id)
    result = await db.execute(
        select(AgentDelivery).where(
            AgentDelivery.production_id == production.id,
            AgentDelivery.idempotency_key == payload.idempotency_key,
        )
    )
    existing = result.scalar_one_or_none()
    if existing is not None:
        if (existing.extra or {}).get("request_signature") != _request_signature(payload):
            raise AppException("幂等键已用于其他交付请求", code=40976, status_code=409)
        return existing
    selected_chapters = _select_chapters(chapters, payload.chapter_ids)
    selected_ids = {item.id for item in selected_chapters}
    selected_storyboards = [item for item in storyboards if item.chapter_id in selected_ids]
    await _lock_review_scope(db, production, selected_chapters, selected_storyboards)
    for storyboard in selected_storyboards:
        await db.refresh(storyboard)
    await _assert_delivery_scope_ready(db, production, selected_chapters, selected_storyboards)
    project = await db.get(Project, production.project_id)
    manifest = _delivery_manifest(
        production,
        selected_chapters,
        selected_storyboards,
        generation_ratio=project.generation_ratio if project else None,
    )
    now = beijing_datetime()
    delivery_extra = {"request_signature": _request_signature(payload)}
    if payload.delivery_type == "jianying_draft":
        delivery_extra.update(
            {
                "platform": payload.platform,
                "jianying_version": payload.jianying_version,
                "draft_name": payload.draft_name or (project.name if project else "灵境漫剧"),
            }
        )
    delivery = AgentDelivery(
        production_id=production.id,
        project_id=production.project_id,
        user_id=user.id,
        delivery_type=payload.delivery_type,
        status="completed" if payload.delivery_type == "manifest" else "pending",
        idempotency_key=payload.idempotency_key,
        manifest=manifest,
        finished_at=now if payload.delivery_type == "manifest" else None,
        extra=delivery_extra,
    )
    db.add(delivery)
    await db.flush()
    db.add(
        AgentEvent(
            production_id=production.id,
            actor_user_id=user.id,
            event_type="delivery.created",
            source="user",
            payload={
                "delivery_id": str(delivery.id),
                "delivery_type": delivery.delivery_type,
                "idempotency_key": delivery.idempotency_key,
            },
        )
    )
    await db.commit()
    await db.refresh(delivery)
    if delivery.delivery_type in {"merged_video", "jianying_draft"}:
        try:
            from app.tasks.agent_delivery import build_agent_delivery

            build_agent_delivery.apply_async(
                args=(str(delivery.id),),
                queue="story_ai_delivery",
                routing_key="story_ai_delivery",
            )
        except Exception as exc:
            await fail_agent_delivery(db, delivery.id, "交付任务入队失败", raw_error=str(exc))
    return delivery


async def create_agent_jianying_export(
    db: AsyncSession,
    production_id: UUID,
    user: User,
    payload: AgentJianyingExportCreateRequest,
) -> Dict[str, Any]:
    delivery = await create_agent_delivery(
        db,
        production_id,
        user,
        AgentDeliveryCreateRequest(
            delivery_type="jianying_draft",
            chapter_ids=payload.chapter_ids,
            platform=payload.platform,
            jianying_version=payload.jianying_version,
            draft_name=payload.draft_name,
            idempotency_key=payload.idempotency_key,
        ),
    )
    return _jianying_export_payload(delivery)


async def list_agent_jianying_exports(
    db: AsyncSession,
    production_id: UUID,
    user_id: UUID,
) -> List[Dict[str, Any]]:
    deliveries = await list_agent_deliveries(db, production_id, user_id)
    return [
        _jianying_export_payload(item)
        for item in deliveries
        if item.delivery_type == "jianying_draft"
    ]


async def get_agent_jianying_export(
    db: AsyncSession,
    production_id: UUID,
    delivery_id: UUID,
    user_id: UUID,
) -> Dict[str, Any]:
    delivery = await get_agent_delivery(db, production_id, delivery_id, user_id)
    if delivery.delivery_type != "jianying_draft":
        raise AppException("剪映草稿导出任务不存在", code=40444, status_code=404)
    return _jianying_export_payload(delivery)


async def list_agent_deliveries(
    db: AsyncSession,
    production_id: UUID,
    user_id: UUID,
) -> List[AgentDelivery]:
    await get_agent_production_or_404(db, production_id, user_id)
    result = await db.execute(
        select(AgentDelivery)
        .where(
            AgentDelivery.production_id == production_id,
            AgentDelivery.user_id == user_id,
        )
        .order_by(AgentDelivery.created_at.desc(), AgentDelivery.id.desc())
    )
    return list(result.scalars().all())


async def get_agent_delivery(
    db: AsyncSession,
    production_id: UUID,
    delivery_id: UUID,
    user_id: UUID,
) -> AgentDelivery:
    await get_agent_production_or_404(db, production_id, user_id)
    result = await db.execute(
        select(AgentDelivery).where(
            AgentDelivery.id == delivery_id,
            AgentDelivery.production_id == production_id,
            AgentDelivery.user_id == user_id,
        )
    )
    delivery = result.scalar_one_or_none()
    if delivery is None:
        raise AppException("交付任务不存在", code=40444, status_code=404)
    return delivery


async def claim_agent_delivery(
    db: AsyncSession,
    delivery_id: UUID,
) -> Tuple[Optional[UUID], int]:
    result = await db.execute(
        select(AgentDelivery).where(AgentDelivery.id == delivery_id).with_for_update()
    )
    delivery = result.scalar_one_or_none()
    if delivery is None or delivery.status == "completed":
        return None, 0
    if delivery.delivery_type not in {"merged_video", "jianying_draft"}:
        return None, 0
    now = beijing_datetime()
    if (
        delivery.status == "running"
        and delivery.lease_expires_at is not None
        and delivery.lease_expires_at > now
    ):
        retry_after = max(1, int((delivery.lease_expires_at - now).total_seconds()) + 1)
        await db.commit()
        return None, retry_after
    token = uuid4()
    delivery.status = "running"
    delivery.lease_token = token
    delivery.lease_expires_at = now + timedelta(
        seconds=max(60, settings.effective_celery_task_time_limit_seconds)
    )
    delivery.attempt_count += 1
    delivery.started_at = now
    delivery.error_summary = None
    await db.commit()
    return token, 0


async def run_agent_delivery(
    db: AsyncSession,
    delivery_id: UUID,
    lease_token: UUID,
) -> None:
    result = await db.execute(
        select(AgentDelivery).where(
            AgentDelivery.id == delivery_id,
            AgentDelivery.status == "running",
            AgentDelivery.lease_token == lease_token,
        )
    )
    delivery = result.scalar_one_or_none()
    if delivery is None:
        return
    urls = [
        str(shot.get("video_url") or "")
        for episode in (delivery.manifest or {}).get("episodes") or []
        for shot in episode.get("shots") or []
    ]
    urls = [url for url in urls if url]
    if not urls:
        raise AppException("交付清单没有可合并的视频", code=40977, status_code=409)
    if delivery.delivery_type == "merged_video":
        output_url = await _merge_and_upload_videos(urls, delivery)
        build_extra = {}
    else:
        from app.services.jianying_drafts import build_and_upload_jianying_draft

        output_url, build_extra = await build_and_upload_jianying_draft(delivery)
    delivery.output_url = output_url
    delivery.status = "completed"
    delivery.finished_at = beijing_datetime()
    delivery.lease_token = None
    delivery.lease_expires_at = None
    delivery.extra = {**(delivery.extra or {}), **build_extra}
    if delivery.delivery_type == "jianying_draft":
        await _complete_production_after_full_jianying_export(db, delivery)
    await db.commit()


async def fail_agent_delivery(
    db: AsyncSession,
    delivery_id: UUID,
    message: str,
    *,
    raw_error: Optional[str] = None,
    lease_token: Optional[UUID] = None,
) -> None:
    delivery = await db.get(AgentDelivery, delivery_id)
    if delivery is None or delivery.status == "completed":
        return
    if lease_token is not None and delivery.lease_token != lease_token:
        return
    delivery.status = "failed"
    delivery.error_summary = sanitize_public_message(message)
    delivery.finished_at = beijing_datetime()
    delivery.extra = {**(delivery.extra or {}), "raw_error": raw_error or message}
    delivery.lease_token = None
    delivery.lease_expires_at = None
    await db.commit()


async def _review_scope(
    db: AsyncSession,
    production_id: UUID,
    user_id: UUID,
) -> Tuple[AgentProduction, List[ProjectChapter], List[ProjectStoryboard]]:
    production = await get_agent_production_or_404(db, production_id, user_id)
    chapter_result = await db.execute(
        select(ProjectChapter)
        .where(
            ProjectChapter.project_id == production.project_id,
            ProjectChapter.user_id == user_id,
            ProjectChapter.is_enabled.is_(True),
            ProjectChapter.extra["agent_production_id"].as_string() == str(production.id),
        )
        .order_by(ProjectChapter.sort_order, ProjectChapter.created_at, ProjectChapter.id)
    )
    chapters = list(chapter_result.scalars().all())
    storyboards: List[ProjectStoryboard] = []
    if chapters:
        storyboard_result = await db.execute(
            select(ProjectStoryboard)
            .where(
                ProjectStoryboard.chapter_id.in_([item.id for item in chapters]),
                ProjectStoryboard.user_id == user_id,
                ProjectStoryboard.is_enabled.is_(True),
            )
            .order_by(
                ProjectStoryboard.chapter_id,
                ProjectStoryboard.shot_number,
                ProjectStoryboard.created_at,
            )
        )
        storyboards = list(storyboard_result.scalars().all())
    return production, chapters, storyboards


async def _issues(db: AsyncSession, production_id: UUID) -> List[AgentReviewIssue]:
    result = await db.execute(
        select(AgentReviewIssue)
        .where(AgentReviewIssue.production_id == production_id)
        .order_by(AgentReviewIssue.created_at, AgentReviewIssue.id)
    )
    return list(result.scalars().all())


async def _episode_reviews(db: AsyncSession, production_id: UUID) -> List[AgentEpisodeReview]:
    result = await db.execute(
        select(AgentEpisodeReview).where(AgentEpisodeReview.production_id == production_id)
    )
    return list(result.scalars().all())


async def _version_counts(
    db: AsyncSession,
    project_id: UUID,
    storyboard_ids: Sequence[UUID],
) -> Dict[Tuple[UUID, str], int]:
    if not storyboard_ids:
        return {}
    result = await db.execute(
        select(
            ProjectGeneratedAsset.target_id,
            ProjectGeneratedAsset.media_type,
            func.count(),
        )
        .where(
            ProjectGeneratedAsset.project_id == project_id,
            ProjectGeneratedAsset.target_type == "storyboard",
            ProjectGeneratedAsset.target_id.in_(storyboard_ids),
            ProjectGeneratedAsset.is_enabled.is_(True),
        )
        .group_by(ProjectGeneratedAsset.target_id, ProjectGeneratedAsset.media_type)
    )
    return {(target_id, media_type): int(count) for target_id, media_type, count in result.all()}


def _storyboard_review_item(
    storyboard: ProjectStoryboard,
    issues: List[AgentReviewIssue],
    version_counts: Dict[Tuple[UUID, str], int],
    production: AgentProduction,
) -> Dict[str, Any]:
    extra = storyboard.extra or {}
    return {
        "storyboard_id": storyboard.id,
        "shot_number": storyboard.shot_number,
        "title": storyboard.title,
        "duration_seconds": storyboard_estimated_duration_seconds(storyboard),
        "image_status": str(extra.get("image_generation_status") or "not_started"),
        "image_url": _media_url(storyboard, "image"),
        "image_history_id": _optional_uuid(extra.get("image_generation_history_id")),
        "image_version_count": version_counts.get((storyboard.id, "image"), 0),
        "video_status": str(extra.get("video_generation_status") or "not_started"),
        "video_url": _media_url(storyboard, "video"),
        "video_history_id": _optional_uuid(extra.get("video_generation_history_id")),
        "video_version_count": version_counts.get((storyboard.id, "video"), 0),
        "issue_count": len(issues),
        "blocking_issue_count": _blocking_count(issues),
    }


def _group_issues(issues: List[AgentReviewIssue], key: str) -> Dict[UUID, List[AgentReviewIssue]]:
    grouped: Dict[UUID, List[AgentReviewIssue]] = {}
    for issue in issues:
        value = getattr(issue, key)
        if value is not None and issue.status == "open":
            grouped.setdefault(value, []).append(issue)
    return grouped


def _blocking_count(issues: Sequence[AgentReviewIssue]) -> int:
    return sum(issue.status == "open" and issue.severity == "blocking" for issue in issues)


def _effective_review_status(review: Optional[AgentEpisodeReview], snapshot_hash: str) -> str:
    if review is None:
        return "pending"
    if review.status == "approved" and review.media_snapshot_hash == snapshot_hash:
        return "approved"
    return "invalidated"


def _media_snapshot_hash(storyboards: Sequence[ProjectStoryboard]) -> str:
    values = []
    for storyboard in storyboards:
        extra = storyboard.extra or {}
        values.append(
            {
                "storyboard_id": str(storyboard.id),
                "image_history_id": str(extra.get("image_generation_history_id") or ""),
                "image_url": _media_url(storyboard, "image") or "",
                "video_history_id": str(extra.get("video_generation_history_id") or ""),
                "video_url": _media_url(storyboard, "video") or "",
                "image_status": str(extra.get("image_generation_status") or ""),
                "image_task_record_id": str(
                    extra.get("image_generation_task_record_id") or ""
                ),
                "video_status": str(extra.get("video_generation_status") or ""),
                "video_task_record_id": str(
                    extra.get("video_generation_task_record_id") or ""
                ),
            }
        )
    encoded = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _media_url(storyboard: ProjectStoryboard, media_type: str) -> Optional[str]:
    value = str((storyboard.extra or {}).get(f"{media_type}_generation_result") or "").strip()
    return value or None


def _missing_video_ids(storyboards: Sequence[ProjectStoryboard]) -> List[UUID]:
    return [
        item.id
        for item in storyboards
        if str((item.extra or {}).get("video_generation_status") or "")
        not in SUCCESS_MEDIA_STATUSES
        or not _media_url(item, "video")
    ]


def _require_reviewable_chapter(
    chapter: ProjectChapter,
    storyboards: Sequence[ProjectStoryboard],
) -> None:
    status = effective_storyboard_episode_status(chapter, len(storyboards))
    if status == "ready":
        return
    raise AppException(
        "该集分镜尚未分析完成，暂时不能查看或修改",
        code=40968,
        status_code=409,
        data={
            "chapter_id": str(chapter.id),
            "storyboard_analysis_status": status,
        },
    )


def _select_chapters(
    chapters: Sequence[ProjectChapter], chapter_ids: Sequence[UUID]
) -> List[ProjectChapter]:
    if not chapter_ids:
        return list(chapters)
    mapping = {item.id: item for item in chapters}
    selected = [mapping[value] for value in chapter_ids if value in mapping]
    if len(selected) != len(chapter_ids):
        raise AppException("部分交付剧集不存在", code=40440, status_code=404)
    return selected


async def _assert_delivery_scope_ready(
    db: AsyncSession,
    production: AgentProduction,
    chapters: Sequence[ProjectChapter],
    storyboards: Sequence[ProjectStoryboard],
) -> None:
    reviews = {item.chapter_id: item for item in await _episode_reviews(db, production.id)}
    unapproved = []
    for chapter in chapters:
        chapter_storyboards = [item for item in storyboards if item.chapter_id == chapter.id]
        if _effective_review_status(
            reviews.get(chapter.id), _media_snapshot_hash(chapter_storyboards)
        ) != "approved":
            unapproved.append(chapter.id)
    missing = _missing_video_ids(storyboards)
    blocking_result = await db.execute(
        select(func.count())
        .select_from(AgentReviewIssue)
        .where(
            AgentReviewIssue.production_id == production.id,
            AgentReviewIssue.chapter_id.in_([item.id for item in chapters]),
            AgentReviewIssue.status == "open",
            AgentReviewIssue.severity == "blocking",
        )
    )
    if unapproved or missing or int(blocking_result.scalar_one() or 0):
        raise AppException(
            "整剧尚未满足交付条件",
            code=40979,
            status_code=409,
            data={
                "unapproved_chapter_ids": [str(value) for value in unapproved],
                "missing_video_storyboard_ids": [str(value) for value in missing],
            },
        )


def _delivery_manifest(
    production: AgentProduction,
    chapters: Sequence[ProjectChapter],
    storyboards: Sequence[ProjectStoryboard],
    *,
    generation_ratio: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "version": 1,
        "production_id": str(production.id),
        "project_id": str(production.project_id),
        "generation_ratio": generation_ratio or "16:9",
        "video_resolution": str(
            (production.production_spec or {}).get("video_resolution") or "1080p"
        ),
        "created_at": beijing_datetime().isoformat(),
        "episodes": [
            {
                "chapter_id": str(chapter.id),
                "episode_number": int((chapter.extra or {}).get("episode_number") or 0),
                "title": chapter.title,
                "shots": [
                    {
                        "storyboard_id": str(storyboard.id),
                        "shot_number": storyboard.shot_number,
                        "group_number": storyboard.shot_number,
                        "title": storyboard.title,
                        "duration_seconds": storyboard_estimated_duration_seconds(storyboard),
                        "video_url": _media_url(storyboard, "video"),
                        "video_history_id": str(
                            (storyboard.extra or {}).get("video_generation_history_id") or ""
                        ),
                    }
                    for storyboard in storyboards
                    if storyboard.chapter_id == chapter.id
                ],
            }
            for chapter in chapters
        ],
    }


def _jianying_export_payload(delivery: AgentDelivery) -> Dict[str, Any]:
    from app.services.jianying_drafts import compatibility_notes

    extra = delivery.extra or {}
    platform = str(extra.get("platform") or "windows")
    return {
        "id": delivery.id,
        "production_id": delivery.production_id,
        "status": delivery.status,
        "platform": platform,
        "jianying_version": str(extra.get("jianying_version") or "10.8"),
        "draft_name": str(extra.get("draft_name") or "灵境漫剧"),
        "episode_count": len((delivery.manifest or {}).get("episodes") or []),
        "video_count": int(extra.get("video_count") or _manifest_video_count(delivery.manifest)),
        "output_url": delivery.output_url,
        "error_summary": delivery.error_summary,
        "compatibility_notes": compatibility_notes(platform),
        "created_at": delivery.created_at,
        "updated_at": delivery.updated_at,
    }


def _manifest_video_count(manifest: Optional[Dict[str, Any]]) -> int:
    return sum(
        len(episode.get("shots") or []) for episode in (manifest or {}).get("episodes") or []
    )


async def _complete_production_after_full_jianying_export(
    db: AsyncSession,
    delivery: AgentDelivery,
) -> None:
    chapter_result = await db.execute(
        select(ProjectChapter.id).where(
            ProjectChapter.project_id == delivery.project_id,
            ProjectChapter.user_id == delivery.user_id,
            ProjectChapter.is_enabled.is_(True),
            ProjectChapter.extra["agent_production_id"].as_string()
            == str(delivery.production_id),
        )
    )
    chapter_ids = set(chapter_result.scalars().all())
    delivered_ids = {
        _optional_uuid(episode.get("chapter_id"))
        for episode in (delivery.manifest or {}).get("episodes") or []
    }
    delivered_ids.discard(None)
    if not chapter_ids or delivered_ids != chapter_ids:
        return
    production = await db.get(AgentProduction, delivery.production_id)
    if production is None or production.status in {"completed", "cancelled"}:
        return
    if production.status != "running":
        production.status = transition_production_status(production.status, "running")
    production.status = transition_production_status(production.status, "completed")
    production.current_stage = "completed"
    production.error_summary = None
    production.lock_version += 1


async def _merge_and_upload_videos(urls: List[str], delivery: AgentDelivery) -> str:
    with tempfile.TemporaryDirectory(prefix="agent-delivery-") as temp_dir:
        root = Path(temp_dir)
        paths = await _download_delivery_clips(urls, root)
        concat_file = root / "concat.txt"
        concat_file.write_text(
            "".join(f"file '{path.as_posix()}'\n" for path in paths),
            encoding="utf-8",
        )
        output = root / "delivery.mp4"
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(output),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=max(60, settings.effective_celery_task_time_limit_seconds)
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            raise AppException("成片合并超时", code=50240, status_code=502)
        if process.returncode != 0 or not output.exists():
            reason = stderr.decode("utf-8", errors="replace")[-1000:]
            raise AppException(f"成片合并失败：{reason}", code=50240, status_code=502)
        with output.open("rb") as fileobj:
            oss = OssClient()
            url, _object_key = await run_in_threadpool(
                oss.upload_fileobj,
                fileobj,
                "delivery.mp4",
                f"agent-deliveries/{delivery.production_id}",
                "video/mp4",
            )
        return url


async def _download_delivery_clips(urls: Sequence[str], root: Path) -> List[Path]:
    allowed_hosts = trusted_oss_hosts()
    timeout = httpx.Timeout(
        settings.generated_media_connect_timeout_seconds,
        read=settings.generated_media_read_timeout_seconds,
    )
    paths = []
    total_size = 0
    max_size = settings.generated_media_download_max_size_mb * 1024 * 1024
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        for index, raw_url in enumerate(urls):
            path = root / f"clip-{index:05d}.mp4"
            response = await open_safe_http_response(
                client,
                raw_url,
                allowed_hosts=allowed_hosts,
            )
            try:
                response.raise_for_status()
                with path.open("wb") as fileobj:
                    async for chunk in response.aiter_bytes():
                        total_size += len(chunk)
                        if total_size > max_size:
                            raise AppException("交付视频总大小超过限制", code=41302, status_code=413)
                        fileobj.write(chunk)
            finally:
                await response.aclose()
            paths.append(path)
    return paths


def _request_signature(payload: Any) -> str:
    encoded = json.dumps(payload.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _image_aspect_ratio(value: str) -> str:
    allowed = {"16:9", "9:16", "1:1", "4:3", "3:4", "3:2", "2:3", "21:9"}
    return value if value in allowed else "16:9"


def _optional_uuid(value: Any) -> Optional[UUID]:
    try:
        return UUID(str(value)) if value else None
    except (TypeError, ValueError):
        return None


async def _lock_production(db: AsyncSession, production_id: UUID, user_id: UUID) -> None:
    result = await db.execute(
        select(AgentProduction.id)
        .join(Project, Project.id == AgentProduction.project_id)
        .where(
            AgentProduction.id == production_id,
            AgentProduction.user_id == user_id,
            Project.user_id == user_id,
            Project.is_enabled.is_(True),
        )
        .with_for_update()
    )
    if result.scalar_one_or_none() is None:
        raise AppException("整剧任务不存在", code=40430, status_code=404)


async def _lock_review_scope(
    db: AsyncSession,
    production: AgentProduction,
    chapters: Sequence[ProjectChapter],
    storyboards: Sequence[ProjectStoryboard],
) -> None:
    await _lock_production(db, production.id, production.user_id)
    chapter_ids = sorted({item.id for item in chapters}, key=str)
    if chapter_ids:
        await db.execute(
            select(ProjectChapter.id)
            .where(
                ProjectChapter.id.in_(chapter_ids),
                ProjectChapter.user_id == production.user_id,
            )
            .order_by(ProjectChapter.id)
            .with_for_update()
        )
    storyboard_ids = sorted({item.id for item in storyboards}, key=str)
    if storyboard_ids:
        await db.execute(
            select(ProjectStoryboard.id)
            .where(
                ProjectStoryboard.id.in_(storyboard_ids),
                ProjectStoryboard.user_id == production.user_id,
            )
            .order_by(ProjectStoryboard.id)
            .with_for_update()
        )
