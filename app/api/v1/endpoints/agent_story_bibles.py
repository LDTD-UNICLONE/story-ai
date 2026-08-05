from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.responses import success
from app.db.session import get_db
from app.models.user import User
from app.schemas.agent_story_bible import (
    AgentAssetCandidateListOut,
    AgentAssetCandidateMaterializeOut,
    AgentAssetCandidateMaterializeRequest,
    AgentAssetCandidateOut,
    AgentAssetCandidateUpdateRequest,
    SeriesBibleConfirmOut,
    SeriesBibleConfirmRequest,
    SeriesBibleUpdateRequest,
    SeriesBibleVersionListOut,
    SeriesBibleVersionOut,
)
from app.services.agent_story_bibles import (
    confirm_story_bible,
    get_current_story_bible,
    initialize_story_bible,
    list_asset_candidates,
    list_story_bible_versions,
    materialize_asset_candidates,
    update_asset_candidate,
    update_story_bible,
)

router = APIRouter()


@router.post("/agent-productions/{production_id}/story-bible/initialize")
async def initialize_my_story_bible(
    production_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    bible = await initialize_story_bible(db, production_id, current_user)
    data = SeriesBibleVersionOut.model_validate(bible)
    return success(data=data.model_dump(mode="json"), message="故事圣经已生成")


@router.get("/agent-productions/{production_id}/story-bible")
async def my_current_story_bible(
    production_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    bible = await get_current_story_bible(db, production_id, current_user.id)
    data = SeriesBibleVersionOut.model_validate(bible)
    return success(data=data.model_dump(mode="json"))


@router.get("/agent-productions/{production_id}/story-bible/versions")
async def my_story_bible_versions(
    production_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    versions = await list_story_bible_versions(db, production_id, current_user.id)
    data = SeriesBibleVersionListOut(
        items=[SeriesBibleVersionOut.model_validate(item) for item in versions]
    )
    return success(data=data.model_dump(mode="json"))


@router.patch("/agent-productions/{production_id}/story-bible")
async def update_my_story_bible(
    production_id: UUID,
    payload: SeriesBibleUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    bible = await update_story_bible(db, production_id, current_user, payload)
    data = SeriesBibleVersionOut.model_validate(bible)
    return success(data=data.model_dump(mode="json"), message="故事圣经已更新")


@router.post("/agent-productions/{production_id}/story-bible/confirm")
async def confirm_my_story_bible(
    production_id: UUID,
    payload: SeriesBibleConfirmRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await confirm_story_bible(db, production_id, current_user, payload)
    data = SeriesBibleConfirmOut.model_validate(result)
    return success(data=data.model_dump(mode="json"), message="故事圣经已确认")


@router.get("/agent-productions/{production_id}/asset-candidates")
async def my_agent_asset_candidates(
    production_id: UUID,
    asset_type: Optional[Literal["character", "scene", "prop"]] = Query(default=None),
    review_status: Optional[Literal["ready", "needs_review", "rejected", "materialized"]] = Query(
        default=None
    ),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    bible_version, candidates = await list_asset_candidates(
        db,
        production_id,
        current_user.id,
        asset_type=asset_type,
        review_status=review_status,
    )
    data = AgentAssetCandidateListOut(
        bible_version=bible_version,
        items=[AgentAssetCandidateOut.model_validate(item) for item in candidates],
        total=len(candidates),
    )
    return success(data=data.model_dump(mode="json"))


@router.patch("/agent-productions/{production_id}/asset-candidates/{candidate_id}")
async def update_my_agent_asset_candidate(
    production_id: UUID,
    candidate_id: UUID,
    payload: AgentAssetCandidateUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    candidate = await update_asset_candidate(
        db,
        production_id,
        candidate_id,
        current_user,
        payload,
    )
    data = AgentAssetCandidateOut.model_validate(candidate)
    return success(data=data.model_dump(mode="json"), message="资产候选已更新")


@router.post("/agent-productions/{production_id}/asset-candidates/materialize")
async def materialize_my_agent_asset_candidates(
    production_id: UUID,
    payload: AgentAssetCandidateMaterializeRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await materialize_asset_candidates(db, production_id, current_user, payload)
    data = AgentAssetCandidateMaterializeOut.model_validate(result)
    return success(data=data.model_dump(mode="json"), message="资产候选已写入项目资产库")
