from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_session
from app.db.repositories.scene_repo import SceneRepository
from app.models.scene import SceneCreate, SceneRead, SceneUpdate
from app.models.scene_thesis import (
    SceneThesisCreate,
    SceneThesisRead,
    SceneThesisUpdate,
)

router = APIRouter(tags=["scenes"])


@router.post(
    "/api/campaigns/{campaign_id}/scenes",
    response_model=SceneRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_scene(
    campaign_id: UUID,
    data: SceneCreate,
    session: AsyncSession = Depends(get_session),
):
    scene = await SceneRepository(session).create(campaign_id, data)
    await session.commit()
    return scene


@router.get("/api/campaigns/{campaign_id}/scenes", response_model=list[SceneRead])
async def list_scenes(
    campaign_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    return await SceneRepository(session).list_by_campaign(campaign_id)


@router.get("/api/scenes/{scene_id}", response_model=SceneRead)
async def get_scene(
    scene_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    scene = await SceneRepository(session).get_by_id(scene_id)
    if not scene:
        raise HTTPException(status_code=404, detail="Scene not found")
    return scene


@router.put("/api/scenes/{scene_id}", response_model=SceneRead)
async def update_scene(
    scene_id: UUID,
    data: SceneUpdate,
    session: AsyncSession = Depends(get_session),
):
    scene = await SceneRepository(session).update(scene_id, data)
    if not scene:
        raise HTTPException(status_code=404, detail="Scene not found")
    await session.commit()
    return scene


@router.delete("/api/scenes/{scene_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_scene(
    scene_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    if not await SceneRepository(session).delete(scene_id):
        raise HTTPException(status_code=404, detail="Scene not found")
    await session.commit()


@router.post("/api/scenes/{scene_id}/participants")
async def add_participant(
    scene_id: UUID,
    entity_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    try:
        await SceneRepository(session).add_participant(scene_id, entity_id)
        await session.commit()
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"success": True}


@router.delete("/api/scenes/{scene_id}/participants/{entity_id}")
async def remove_participant(
    scene_id: UUID,
    entity_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    if not await SceneRepository(session).remove_participant(scene_id, entity_id):
        raise HTTPException(status_code=404, detail="Participant not found in scene")
    await session.commit()
    return {"success": True}


@router.get("/api/scenes/{scene_id}/participants", response_model=list[UUID])
async def list_participants(
    scene_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    return await SceneRepository(session).get_participants(scene_id)


@router.post(
    "/api/scenes/{scene_id}/theses",
    response_model=SceneThesisRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_thesis(
    scene_id: UUID,
    data: SceneThesisCreate,
    turn_id: UUID | None = None,
    session: AsyncSession = Depends(get_session),
):
    thesis = await SceneRepository(session).create_thesis(
        scene_id,
        data,
        source_turn_id=turn_id,
    )
    await session.commit()
    return thesis


@router.get("/api/scenes/{scene_id}/theses", response_model=list[SceneThesisRead])
async def list_theses(
    scene_id: UUID,
    active_only: bool = True,
    session: AsyncSession = Depends(get_session),
):
    return await SceneRepository(session).list_theses_by_scene(scene_id, active_only)


@router.put("/api/theses/{thesis_id}", response_model=SceneThesisRead)
async def update_thesis(
    thesis_id: UUID,
    data: SceneThesisUpdate,
    session: AsyncSession = Depends(get_session),
):
    thesis = await SceneRepository(session).update_thesis(thesis_id, data)
    if not thesis:
        raise HTTPException(status_code=404, detail="Thesis not found")
    await session.commit()
    return thesis


@router.put("/api/theses/{thesis_id}/pin", response_model=SceneThesisRead)
async def pin_thesis(
    thesis_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    repo = SceneRepository(session)
    if not await repo.pin_thesis(thesis_id):
        raise HTTPException(status_code=404, detail="Thesis not found")
    await session.commit()
    return await repo.get_thesis_by_id(thesis_id)


@router.put("/api/theses/{thesis_id}/resolve", response_model=SceneThesisRead)
async def resolve_thesis(
    thesis_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    repo = SceneRepository(session)
    if not await repo.resolve_thesis(thesis_id):
        raise HTTPException(status_code=404, detail="Thesis not found")
    await session.commit()
    return await repo.get_thesis_by_id(thesis_id)


@router.delete("/api/theses/{thesis_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_thesis(
    thesis_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    if not await SceneRepository(session).delete_thesis(thesis_id):
        raise HTTPException(status_code=404, detail="Thesis not found")
    await session.commit()
