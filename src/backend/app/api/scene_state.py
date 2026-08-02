from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_session
from app.models.scene_state import (
    LocationExitCreate,
    LocationExitRead,
    SceneStateRead,
    SceneStateUpdate,
    SceneStateValidation,
)
from app.services.scene_state_service import SceneStateService

router = APIRouter(tags=["scene-state"])


@router.get(
    "/api/campaigns/{campaign_id}/scenes/{scene_id}/state",
    response_model=SceneStateRead,
)
async def get_scene_state(
    campaign_id: UUID,
    scene_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    try:
        return await SceneStateService(session).get(campaign_id, scene_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put(
    "/api/campaigns/{campaign_id}/scenes/{scene_id}/state",
    response_model=SceneStateRead,
)
async def update_scene_state(
    campaign_id: UUID,
    scene_id: UUID,
    data: SceneStateUpdate,
    session: AsyncSession = Depends(get_session),
):
    try:
        result = await SceneStateService(session).update(campaign_id, scene_id, data)
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    return result


@router.post(
    "/api/campaigns/{campaign_id}/scenes/{scene_id}/state/validate",
    response_model=SceneStateValidation,
)
async def validate_scene_state(
    campaign_id: UUID,
    scene_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    try:
        return await SceneStateService(session).validate(campaign_id, scene_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/api/campaigns/{campaign_id}/locations/{location_id}/exits",
    response_model=list[LocationExitRead],
)
async def list_location_exits(
    campaign_id: UUID,
    location_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    try:
        return await SceneStateService(session).list_exits(campaign_id, location_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/api/campaigns/{campaign_id}/locations/{location_id}/exits",
    response_model=list[LocationExitRead],
    status_code=status.HTTP_201_CREATED,
)
async def create_location_exit(
    campaign_id: UUID,
    location_id: UUID,
    data: LocationExitCreate,
    session: AsyncSession = Depends(get_session),
):
    try:
        result = await SceneStateService(session).create_exit(
            campaign_id,
            location_id,
            data,
        )
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    return result
