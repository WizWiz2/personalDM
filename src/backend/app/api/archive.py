from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_session
from app.services.campaign_archive import (
    CampaignArchiveService,
    backup_database,
)
from app.services.world_state_snapshot import WorldStateSnapshotService


router = APIRouter(prefix="/api/campaigns/{campaign_id}", tags=["archive"])
import_router = APIRouter(prefix="/api/campaigns", tags=["archive"])


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
    return {"path": str(path), "campaign": archive, "archive": archive}


@import_router.post("/import")
async def import_campaign(
    archive: dict = Body(...),
    replace: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
):
    try:
        return await CampaignArchiveService(session).import_archive(
            archive, replace=replace
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/world-state/initial")
async def get_initial_world_state(
    campaign_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    snapshot = await WorldStateSnapshotService(session).get(campaign_id)
    return {"campaign_id": str(campaign_id), "snapshot": snapshot}


@router.post("/world-state/initial")
async def capture_initial_world_state(
    campaign_id: UUID,
    replace: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
):
    try:
        snapshot = await WorldStateSnapshotService(session).capture(
            campaign_id, replace=replace
        )
        await session.commit()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"campaign_id": str(campaign_id), "snapshot": snapshot}


@router.post("/canon/rebuild")
async def rebuild_canon(
    campaign_id: UUID,
    apply: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
):
    try:
        return await CampaignArchiveService(session).rebuild_canon(
            campaign_id, apply=apply
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
