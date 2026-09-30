from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence
from uuid import UUID

from app.core.exceptions import AppException


READY_STATUSES = {"success", "selected"}
ACTIVE_STATUSES = {"pending", "running"}


@dataclass(frozen=True)
class StoryboardEpisodeProgress:
    visible_chapter_ids: tuple[UUID, ...]
    current_chapter_id: Optional[UUID]
    current_status: Optional[str]
    current_task_record_id: Optional[UUID]
    failed_chapter_ids: tuple[UUID, ...]
    total_episode_count: int

    @property
    def completed_episode_count(self) -> int:
        return len(self.visible_chapter_ids)

    @property
    def remaining_episode_count(self) -> int:
        return self.total_episode_count - self.completed_episode_count

    @property
    def analysis_complete(self) -> bool:
        return self.completed_episode_count == self.total_episode_count


def build_storyboard_episode_progress(
    chapters: Sequence[Any],
    storyboard_counts: Mapping[UUID, int],
) -> StoryboardEpisodeProgress:
    """Return every episode whose independently generated storyboard is usable."""

    visible: list[UUID] = []
    current_chapter_id: Optional[UUID] = None
    current_status: Optional[str] = None
    current_task_record_id: Optional[UUID] = None
    failed: list[UUID] = []

    for chapter in chapters:
        status = effective_storyboard_episode_status(
            chapter,
            storyboard_counts.get(chapter.id, 0),
        )
        if status == "ready":
            visible.append(chapter.id)
            continue
        if status == "failed":
            failed.append(chapter.id)
        if current_chapter_id is None:
            current_chapter_id = chapter.id
            current_status = status
            current_task_record_id = _optional_uuid(
                (chapter.extra or {}).get("storyboard_analysis_task_record_id")
            )

    return StoryboardEpisodeProgress(
        visible_chapter_ids=tuple(visible),
        current_chapter_id=current_chapter_id,
        current_status=current_status,
        current_task_record_id=current_task_record_id,
        failed_chapter_ids=tuple(failed),
        total_episode_count=len(chapters),
    )


def effective_storyboard_episode_status(chapter: Any, storyboard_count: int) -> str:
    extra = chapter.extra or {}
    stored_status = extra.get("storyboard_analysis_status")
    raw_status = str(stored_status or "not_started")
    expected_fingerprint = str(
        extra.get("storyboard_analysis_input_fingerprint") or ""
    )
    result_fingerprint = str(
        extra.get("storyboard_analysis_result_fingerprint") or ""
    )
    if (
        expected_fingerprint
        and result_fingerprint
        and expected_fingerprint != result_fingerprint
    ):
        return "stale"
    if raw_status in READY_STATUSES:
        return "ready" if storyboard_count > 0 else "invalid"
    if stored_status in (None, "") and storyboard_count > 0:
        return "ready"
    return raw_status


def require_visible_storyboard_episode(
    progress: StoryboardEpisodeProgress,
    chapter_id: UUID,
) -> None:
    if chapter_id in progress.visible_chapter_ids:
        return
    raise AppException(
        "该集分镜尚未分析完成，暂时不能查看或修改",
        code=40968,
        status_code=409,
        data={
            "chapter_id": str(chapter_id),
            "current_chapter_id": (
                str(progress.current_chapter_id)
                if progress.current_chapter_id is not None
                else None
            ),
            "current_status": progress.current_status,
            "completed_episode_count": progress.completed_episode_count,
        },
    )


def failed_storyboard_chapter_ids(chapters: Iterable[Any]) -> tuple[UUID, ...]:
    return tuple(
        chapter.id
        for chapter in chapters
        if str((chapter.extra or {}).get("storyboard_analysis_status") or "not_started")
        == "failed"
    )


def _optional_uuid(value: Any) -> Optional[UUID]:
    if value in (None, ""):
        return None
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None
