from __future__ import annotations

from dataclasses import asdict
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_session
from app.services.visual_generation import ComfyUIError, VisualGenerationService

router = APIRouter(tags=["visuals"])


def _asset_payload(service: VisualGenerationService, path, kind: str) -> dict:
    return {
        "kind": kind,
        "available": path.is_file(),
        "url": service.public_url(path),
    }


def _generated_payload(result) -> dict:
    payload = asdict(result)
    payload["available"] = True
    return payload


@router.get("/api/visuals/status")
async def visual_status(session: AsyncSession = Depends(get_session)):
    return await VisualGenerationService(session).status()


@router.get("/api/characters/{character_id}/visuals/portrait")
async def get_character_portrait(
    character_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    service = VisualGenerationService(session)
    return _asset_payload(
        service,
        service.character_portrait_path(character_id),
        service.PORTRAIT_TYPE,
    )


@router.post("/api/characters/{character_id}/visuals/portrait")
async def generate_character_portrait(
    character_id: UUID,
    force: bool = Query(default=True),
    session: AsyncSession = Depends(get_session),
):
    try:
        result = await VisualGenerationService(session).generate_character_portrait(
            character_id,
            force=force,
        )
        return _generated_payload(result)
    except ComfyUIError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/campaigns/{campaign_id}/visuals/cover")
async def get_campaign_cover(
    campaign_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    service = VisualGenerationService(session)
    return _asset_payload(
        service,
        service.campaign_cover_path(campaign_id),
        service.CAMPAIGN_COVER_TYPE,
    )


@router.post("/api/campaigns/{campaign_id}/visuals/cover")
async def generate_campaign_cover(
    campaign_id: UUID,
    force: bool = Query(default=True),
    session: AsyncSession = Depends(get_session),
):
    try:
        result = await VisualGenerationService(session).generate_campaign_cover(
            campaign_id,
            force=force,
        )
        return _generated_payload(result)
    except ComfyUIError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/campaigns/{campaign_id}/scenes/{scene_id}/visuals/latest")
async def get_scene_visual(
    campaign_id: UUID,
    scene_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    service = VisualGenerationService(session)
    # Validate ownership even when there is no generated file yet.
    try:
        from app.services.scene_state_service import SceneStateService

        await SceneStateService(session).get(campaign_id, scene_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _asset_payload(service, service.scene_path(scene_id), service.SCENE_TYPE)


@router.post("/api/campaigns/{campaign_id}/scenes/{scene_id}/visuals")
async def generate_scene_visual(
    campaign_id: UUID,
    scene_id: UUID,
    force: bool = Query(default=True),
    session: AsyncSession = Depends(get_session),
):
    try:
        result = await VisualGenerationService(session).generate_scene(
            campaign_id,
            scene_id,
            force=force,
        )
        return _generated_payload(result)
    except ComfyUIError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
