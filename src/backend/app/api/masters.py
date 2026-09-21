"""Game Master preset listing and per-campaign selection."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_session
from app.models.game_master import (
    CampaignMasterRead,
    GameMasterPersona,
    SetCampaignMasterRequest,
)
from app.services.master_service import MasterService

router = APIRouter(tags=["game-masters"])


@router.get("/api/game-masters/presets", response_model=list[GameMasterPersona])
async def list_game_master_presets() -> list[GameMasterPersona]:
    return MasterService.list_presets()


@router.get(
    "/api/campaigns/{campaign_id}/game-master",
    response_model=CampaignMasterRead,
)
async def get_campaign_game_master(
    campaign_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> CampaignMasterRead:
    try:
        return await MasterService(session).get(campaign_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put(
    "/api/campaigns/{campaign_id}/game-master",
    response_model=CampaignMasterRead,
)
async def set_campaign_game_master(
    campaign_id: UUID,
    request: SetCampaignMasterRequest,
    session: AsyncSession = Depends(get_session),
) -> CampaignMasterRead:
    try:
        result = await MasterService(session).set_master(campaign_id, request)
        await session.commit()
        return result
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
