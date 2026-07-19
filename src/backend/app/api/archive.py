from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_session
from app.services.campaign_archive import (
    CampaignArchiveService,
    backup_database,
)
from app.services.initial_world_state import InitialWorldStateService


router = APIRouter(prefix="/api/campaigns/{campaign_id}", tags=["archive"])


@router.post("/backup")
async def create_backup(campaign_id: UUID):
    try:
        path = backup_database(f"campaign-{campaign_id}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"success": True, "path": str(path)}


@router.get("/export")
async def export_campaign(
    campaign_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    try:
        path, snapshot = await CampaignArchiveService(session).export_json(campaign_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"path": str(path), "campaign": snapshot}


@router.get("/initial-world-state")
async def get_initial_world_state(
    campaign_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    state = await InitialWorldStateService(session).get(campaign_id)
    return {"campaign_id": str(campaign_id), "initial_world_state": state}


@router.post("/initial-world-state/capture")
async def capture_initial_world_state(
    campaign_id: UUID,
    replace: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
):
    state = await CampaignArchiveService(session).capture_initial_state(
        campaign_id,
        replace=replace,
    )
    await session.commit()
    return {"campaign_id": str(campaign_id), **state}


@router.post("/canon/rebuild")
async def rebuild_canon(
    campaign_id: UUID,
    apply: bool = Query(default=False),
    verify: bool = Query(default=True),
    session: AsyncSession = Depends(get_session),
):
    try:
        return await CampaignArchiveService(session).rebuild_canon(
            campaign_id,
            apply=apply,
            verify=verify,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
