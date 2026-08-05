from typing import Any, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_core_asset import AgentCoreAssetLock
from app.models.agent_production import AgentEvent, AgentProduction


async def track_core_asset_reference_change(
    db: AsyncSession,
    *,
    project_id: UUID,
    user_id: UUID,
    asset_type: str,
    asset_id: UUID,
    variant_id: Optional[UUID] = None,
    previous_reference_image: Any,
    new_reference_image: Any,
    source: str = "user",
) -> None:
    previous = str(previous_reference_image or "").strip()
    current = str(new_reference_image or "").strip()
    if previous == current:
        return
    result = await db.execute(
        select(AgentCoreAssetLock, AgentProduction)
        .join(AgentProduction, AgentProduction.id == AgentCoreAssetLock.production_id)
        .where(
            AgentCoreAssetLock.project_id == project_id,
            AgentCoreAssetLock.status == "active",
            AgentProduction.user_id == user_id,
        )
        .with_for_update(of=AgentProduction)
    )
    for lock_record, production in result.all():
        snapshot = next(
            (
                item
                for item in lock_record.assets or []
                if item.get("asset_type") == asset_type
                and str(item.get("asset_id")) == str(asset_id)
            ),
            None,
        )
        if snapshot is None:
            continue
        variant_snapshot = None
        if variant_id is not None:
            variant_snapshot = next(
                (
                    item
                    for item in snapshot.get("variants") or []
                    if str(item.get("variant_id")) == str(variant_id)
                ),
                None,
            )
            if variant_snapshot is None:
                continue
        extra = dict(production.extra or {})
        pending = list(extra.get("core_asset_pending_changes") or [])
        remaining = [
            item
            for item in pending
            if not (
                item.get("asset_type") == asset_type
                and str(item.get("asset_id")) == str(asset_id)
                and str(item.get("variant_id") or "") == str(variant_id or "")
            )
        ]
        locked_reference = str(
            (variant_snapshot or snapshot).get("reference_image") or ""
        ).strip()
        if locked_reference == current:
            if len(remaining) == len(pending):
                continue
            extra["core_asset_pending_changes"] = remaining
            if not remaining:
                return_stage = extra.pop(
                    "core_asset_change_review_return_stage",
                    "pilot_production",
                )
                if production.current_stage == "core_asset_change_review":
                    production.current_stage = return_stage
            production.extra = extra
            production.lock_version += 1
            db.add(
                AgentEvent(
                    production_id=production.id,
                    actor_user_id=user_id,
                    event_type="core_asset.reference_change_reverted",
                    source=source,
                    payload={
                        "asset_type": asset_type,
                        "asset_id": str(asset_id),
                        **({"variant_id": str(variant_id)} if variant_id else {}),
                        "lock_version": lock_record.version,
                        "reference_image": current or None,
                    },
                )
            )
            continue
        pending = remaining
        pending.append(
            {
                "asset_type": asset_type,
                "asset_id": str(asset_id),
                **({"variant_id": str(variant_id)} if variant_id else {}),
                "lock_version": lock_record.version,
                "previous_reference_image": previous or None,
                "new_reference_image": current or None,
            }
        )
        extra["core_asset_pending_changes"] = pending
        if production.status not in {"completed", "cancelled"}:
            if production.current_stage != "core_asset_change_review":
                extra["core_asset_change_review_return_stage"] = production.current_stage
            production.current_stage = "core_asset_change_review"
            production.lock_version += 1
        production.extra = extra
        db.add(
            AgentEvent(
                production_id=production.id,
                actor_user_id=user_id,
                event_type="core_asset.reference_changed",
                source=source,
                payload=pending[-1],
            )
        )
