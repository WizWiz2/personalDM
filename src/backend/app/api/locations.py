from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_session
from app.db.repositories.location_repo import LocationRepository
from app.models.location import LocationCreate, LocationRead, LocationUpdate


router = APIRouter(tags=["locations"])


@router.post(
    "/api/campaigns/{campaign_id}/locations",
    response_model=LocationRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_location(
    campaign_id: UUID,
    data: LocationCreate,
    session: AsyncSession = Depends(get_session),
):
    try:
        location = await LocationRepository(session).create(campaign_id, data)
        await session.commit()
        return location
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/api/campaigns/{campaign_id}/locations",
    response_model=list[LocationRead],
)
async def list_locations(
    campaign_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    return await LocationRepository(session).list_by_campaign(campaign_id)


@router.get("/api/locations/{location_id}", response_model=LocationRead)
async def get_location(
    location_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    location = await LocationRepository(session).get_by_id(location_id)
    if not location:
        raise HTTPException(status_code=404, detail="Location not found")
    return location


@router.get(
    "/api/locations/{location_id}/ancestry",
    response_model=list[LocationRead],
)
async def get_location_ancestry(
    location_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    location = await LocationRepository(session).get_by_id(location_id)
    if not location:
        raise HTTPException(status_code=404, detail="Location not found")
    return await LocationRepository(session).ancestry(location_id)


@router.put("/api/locations/{location_id}", response_model=LocationRead)
async def update_location(
    location_id: UUID,
    data: LocationUpdate,
    session: AsyncSession = Depends(get_session),
):
    try:
        location = await LocationRepository(session).update(location_id, data)
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not location:
        raise HTTPException(status_code=404, detail="Location not found")
    await session.commit()
    return location
