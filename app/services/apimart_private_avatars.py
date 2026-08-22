import hashlib
from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import AppException
from app.core.timezone import beijing_datetime
from app.integrations import apimart
from app.models.apimart_private_avatar import ApimartPrivateAvatarAsset
from app.models.task_record import UserTaskRecord


PRIVATE_AVATAR_STAGE = "private_avatar_review"
PRIVATE_AVATAR_MODELS = frozenset(
    {
        "seedance-2.0",
        "seedance-2.0-face",
        "seedance-2.0-fast",
        "seedance-2.0-fast-face",
        "seedance-2.0-mini",
        "seedance-2-0",
        "seedance-2.5",
    }
)


@dataclass(frozen=True)
class PrivateAvatarReviewResult:
    failed_reason: Optional[str]
    approved_urls: Dict[str, str]


def is_private_avatar_stage(record: UserTaskRecord) -> bool:
    return str((record.extra or {}).get("provider_stage") or "") == PRIVATE_AVATAR_STAGE


def private_avatar_resume_required(record: UserTaskRecord) -> bool:
    return bool((record.extra or {}).get("private_avatar_resume_required"))


def mark_private_avatar_resume_scheduled(record: UserTaskRecord) -> None:
    extra = dict(record.extra or {})
    extra.pop("private_avatar_resume_required", None)
    record.extra = extra


async def prepare_private_avatar_references(
    db: AsyncSession,
    task_record: UserTaskRecord,
    model_id: str,
    model_extra: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    normalized_model = str(model_id or "").strip().lower()
    if normalized_model not in PRIVATE_AVATAR_MODELS:
        return model_extra

    source_urls = _private_avatar_source_urls(task_record, normalized_model, model_extra)
    if not source_urls:
        return model_extra

    fingerprints = {_source_fingerprint(url): url for url in source_urls}
    result = await db.execute(
        select(ApimartPrivateAvatarAsset).where(
            ApimartPrivateAvatarAsset.user_id == task_record.user_id,
            ApimartPrivateAvatarAsset.source_fingerprint.in_(fingerprints),
        )
    )
    records = {item.source_fingerprint: item for item in result.scalars().all()}
    failed = [item for item in records.values() if item.status == "failed"]
    if failed:
        raise AppException(
            "人物参考图未通过 Seedance 素材审核，请更换图片后重试",
            code=40019,
            status_code=400,
            data={"failed_images": [item.source_url for item in failed]},
        )

    missing = [
        (fingerprint, source_url)
        for fingerprint, source_url in fingerprints.items()
        if fingerprint not in records
    ]
    if missing:
        provider_payload = await apimart.create_private_avatar_assets(
            [
                {"url": source_url, "name": f"avatar-{fingerprint[:12]}"}
                for fingerprint, source_url in missing
            ],
            group_name=f"story-ai-{str(task_record.user_id).replace('-', '')[:16]}",
            project_name="story-ai",
            model="seedance-2.5" if normalized_model == "seedance-2.5" else None,
        )
        provider_data = _provider_data(provider_payload)
        provider_task_id = str(provider_data.get("id") or "").strip()
        if not provider_task_id:
            raise AppException(
                "APIMart 人像素材审核响应未返回任务 ID",
                code=50232,
                status_code=502,
            )
        statement = insert(ApimartPrivateAvatarAsset).values(
            [
                {
                    "user_id": task_record.user_id,
                    "source_fingerprint": fingerprint,
                    "source_url": source_url,
                    "source_name": f"avatar-{fingerprint[:12]}",
                    "provider_task_id": provider_task_id,
                    "status": "processing",
                    "progress_percent": _progress_percent(provider_data),
                    "extra": {"submission_index": index},
                }
                for index, (fingerprint, source_url) in enumerate(missing)
            ]
        )
        statement = statement.on_conflict_do_nothing(
            index_elements=["user_id", "source_fingerprint"]
        )
        await db.execute(statement)
        await db.flush()
        _attach_review_task(
            task_record,
            provider_task_id=provider_task_id,
            provider_status=str(provider_data.get("status") or "processing"),
            progress_percent=_progress_percent(provider_data),
            required_fingerprints=list(fingerprints),
        )
        return None

    processing = [item for item in records.values() if item.status == "processing"]
    if processing:
        active = processing[0]
        _attach_review_task(
            task_record,
            provider_task_id=active.provider_task_id,
            provider_status="processing",
            progress_percent=active.progress_percent,
            required_fingerprints=list(fingerprints),
        )
        return None

    approved = {
        item.source_url: str(item.provider_asset_url)
        for item in records.values()
        if item.status == "ready" and item.provider_asset_url
    }
    if len(approved) != len(fingerprints):
        raise AppException(
            "人物参考图素材审核状态不完整，请稍后重试",
            code=50231,
            status_code=502,
        )
    return _replace_urls(model_extra, approved)


async def complete_private_avatar_review(
    db: AsyncSession,
    record: UserTaskRecord,
    provider_extra: Dict[str, Any],
) -> PrivateAvatarReviewResult:
    review = dict((record.extra or {}).get("private_avatar") or {})
    provider_task_id = str(review.get("task_id") or record.provider_task_id or "")
    result = await db.execute(
        select(ApimartPrivateAvatarAsset).where(
            ApimartPrivateAvatarAsset.provider_task_id == provider_task_id
        )
    )
    assets = list(result.scalars().all())
    assets.sort(key=lambda item: int((item.extra or {}).get("submission_index") or 0))

    provider_response = provider_extra.get("provider_response") or {}
    provider_result = (
        provider_response.get("result") if isinstance(provider_response, dict) else {}
    ) or {}
    returned_assets = provider_result.get("assets") or []
    if not returned_assets and provider_result.get("asset_url"):
        returned_assets = [provider_result]

    provider_status = str(provider_extra.get("task_status") or "").lower()
    for index, asset in enumerate(assets):
        returned = returned_assets[index] if index < len(returned_assets) else {}
        returned_status = str(returned.get("status") or "").lower()
        asset_url = str(returned.get("asset_url") or "").strip()
        if returned_status == "active" and asset_url:
            asset.status = "ready"
            asset.provider_asset_id = str(returned.get("asset_id") or "") or None
            asset.provider_asset_url = asset_url
            asset.progress_percent = 100
            asset.error_message = None
        elif provider_status in {
            "success",
            "succeeded",
            "completed",
            "complete",
            "finished",
            "done",
            "failed",
            "cancelled",
            "canceled",
        }:
            asset.status = "failed"
            asset.progress_percent = 100
            asset.error_message = "素材审核未通过"

    required_fingerprints = list(review.get("required_fingerprints") or [])
    required_result = await db.execute(
        select(ApimartPrivateAvatarAsset).where(
            ApimartPrivateAvatarAsset.user_id == record.user_id,
            ApimartPrivateAvatarAsset.source_fingerprint.in_(required_fingerprints),
        )
    )
    required = list(required_result.scalars().all())
    failed = [item for item in required if item.status == "failed"]
    approved = {
        item.source_url: str(item.provider_asset_url)
        for item in required
        if item.status == "ready" and item.provider_asset_url
    }
    if failed:
        return PrivateAvatarReviewResult(
            failed_reason="人物参考图未通过 Seedance 素材审核，请更换图片后重试",
            approved_urls=approved,
        )
    return PrivateAvatarReviewResult(failed_reason=None, approved_urls=approved)


def update_private_avatar_progress(
    record: UserTaskRecord,
    provider_extra: Dict[str, Any],
) -> None:
    progress = _progress_percent(provider_extra.get("provider_response") or provider_extra)
    private_avatar = dict((record.extra or {}).get("private_avatar") or {})
    private_avatar.update(
        {
            "status": str(provider_extra.get("task_status") or "processing"),
            "progress_percent": progress,
        }
    )
    record.extra = {**(record.extra or {}), "private_avatar": private_avatar}
    record.provider_status = str(provider_extra.get("task_status") or "processing")


def mark_private_avatar_ready_for_resume(record: UserTaskRecord) -> None:
    private_avatar = dict((record.extra or {}).get("private_avatar") or {})
    private_avatar.update({"status": "completed", "progress_percent": 100})
    extra = dict(record.extra or {})
    extra.update(
        {
            "provider_stage": "video_generation_pending",
            "private_avatar": private_avatar,
            "private_avatar_resume_required": True,
        }
    )
    for key in (
        "progress_percent",
        "last_provider_task_status",
        "model_result_extra",
        "assistant_message_extra",
        "task_id",
        "provider_task_id",
    ):
        extra.pop(key, None)
    record.extra = extra
    record.status = "pending"
    record.provider_task_id = None
    record.provider_status = None
    record.provider_submitted_at = None
    record.last_reconcile_at = beijing_datetime()
    record.next_reconcile_at = None
    record.reconcile_attempts = 0


def _private_avatar_source_urls(
    task_record: UserTaskRecord,
    model_id: str,
    model_extra: Dict[str, Any],
) -> List[str]:
    urls: List[str] = []
    for item in (task_record.extra or {}).get("agent_reference_manifest") or []:
        if isinstance(item, dict) and item.get("asset_type") == "character":
            urls.extend(_urls(item.get("url")))
    urls.extend(_urls(model_extra.get("private_avatar_image_urls")))
    if model_extra.get("private_avatar") is True or model_id.endswith("-face"):
        for key in ("image_urls", "images", "reference_images"):
            urls.extend(_urls(model_extra.get(key)))
        for item in model_extra.get("image_with_roles") or []:
            if isinstance(item, dict):
                urls.extend(_urls(item.get("url") or item.get("image_url")))
    return list(
        dict.fromkeys(url for url in urls if urlsplit(url).scheme.lower() in {"http", "https"})
    )


def _attach_review_task(
    record: UserTaskRecord,
    *,
    provider_task_id: str,
    provider_status: str,
    progress_percent: Optional[int],
    required_fingerprints: List[str],
) -> None:
    now = beijing_datetime()
    record.status = "running"
    record.provider_vendor = apimart.APIMART_VENDOR
    record.provider_task_id = provider_task_id
    record.provider_status = provider_status or "processing"
    record.provider_submitted_at = record.provider_submitted_at or now
    record.next_reconcile_at = now + timedelta(
        seconds=max(5, settings.provider_task_poll_interval_seconds)
    )
    record.extra = {
        **(record.extra or {}),
        "provider_stage": PRIVATE_AVATAR_STAGE,
        "private_avatar": {
            "task_id": provider_task_id,
            "status": provider_status or "processing",
            "progress_percent": progress_percent,
            "required_fingerprints": required_fingerprints,
        },
    }


def _source_fingerprint(url: str) -> str:
    return hashlib.sha256(url.strip().encode("utf-8")).hexdigest()


def _provider_data(payload: Dict[str, Any]) -> Dict[str, Any]:
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def _progress_percent(value: Any) -> Optional[int]:
    if not isinstance(value, dict):
        return None
    raw = value.get("progress")
    try:
        progress = int(float(raw))
    except (TypeError, ValueError):
        return None
    return progress if 0 <= progress <= 100 else None


def _urls(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [url for item in value for url in _urls(item)]
    if isinstance(value, dict):
        for key in ("url", "image_url", "file_url", "oss_url"):
            if key in value:
                return _urls(value[key])
    return []


def _replace_urls(value: Dict[str, Any], mapping: Dict[str, str]) -> Dict[str, Any]:
    def replace(item: Any) -> Any:
        if isinstance(item, str):
            return mapping.get(item, item)
        if isinstance(item, list):
            return [replace(child) for child in item]
        if isinstance(item, dict):
            return {key: replace(child) for key, child in item.items()}
        return item

    return replace(deepcopy(value))
