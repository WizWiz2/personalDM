from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_session
from app.services.campaign_archive import CampaignArchiveService, backup_database
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
        path, archive = await CampaignArchiveService(session).export_json(campaign_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "path": str(path),
        "archive": archive,
        "campaign": archive["campaign"],
    }


@router.get("/canon/state")
async def inspect_replay_state(
    campaign_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    try:
        return await InitialWorldStateService(session).describe(campaign_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/canon/checkpoint")
async def ensure_replay_checkpoint(
    campaign_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    try:
        snapshot = await InitialWorldStateService(session).ensure_snapshot(campaign_id)
        await session.commit()
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "success": True,
        "snapshot": snapshot,
        "snapshot_hash": InitialWorldStateService.digest(snapshot),
    }


@router.post("/canon/rebuild")
async def rebuild_canon(
    campaign_id: UUID,
    apply: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
):
    try:
        return await CampaignArchiveService(session).rebuild_canon(
            campaign_id,
            apply=apply,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
