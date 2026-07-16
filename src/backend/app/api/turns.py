from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.engine import get_session
from app.db.repositories.turn_repo import TurnRepository
from app.models.turn import TurnCreate, TurnRead
from app.services.turn_runner import TurnRunner

router = APIRouter(prefix="/api/campaigns/{campaign_id}/turns", tags=["turns"])

@router.post("", response_class=StreamingResponse)
async def send_turn(
    campaign_id: UUID,
    data: TurnCreate,
    session: AsyncSession = Depends(get_session)
):
    runner = TurnRunner(session)
    async def token_generator():
        async for token in runner.run_turn_stream(campaign_id, data):
            yield token
    return StreamingResponse(token_generator(), media_type="text/event-stream")

@router.post("/stop")
async def stop_generation(campaign_id: UUID):
    success = TurnRunner.stop_generation(campaign_id)
    return {"success": success}

@router.get("", response_model=list[TurnRead])
async def get_history(
    campaign_id: UUID,
    limit: int = 50,
    active_only: bool = True,
    session: AsyncSession = Depends(get_session)
):
    repo = TurnRepository(session)
    return await repo.get_history(campaign_id, limit, active_only)

@router.post("/undo")
async def undo_last_pair(campaign_id: UUID, session: AsyncSession = Depends(get_session)):
    repo = TurnRepository(session)
    success = await repo.undo_last_pair(campaign_id)
    if not success:
        raise HTTPException(status_code=400, detail="No active turns to undo")
    # Commit changes from undo
    await session.commit()
    return {"success": True}

@router.post("/{turn_id}/regenerate", response_class=StreamingResponse)
async def regenerate_turn(
    campaign_id: UUID,
    turn_id: UUID,
    session: AsyncSession = Depends(get_session)
):
    repo = TurnRepository(session)
    # 1. Fetch assistant turn to regenerate
    assistant_turn = await repo.get_by_id(turn_id)
    if not assistant_turn or assistant_turn.role != "assistant":
        raise HTTPException(status_code=404, detail="Assistant turn to regenerate not found")

    # 2. Mark this turn as alternative
    await repo.mark_alternative(turn_id)
    
    # 3. Find parent user turn
    if not assistant_turn.parent_turn_id:
        raise HTTPException(status_code=400, detail="Cannot regenerate a turn without a parent user turn")
        
    user_turn = await repo.get_by_id(assistant_turn.parent_turn_id)
    if not user_turn:
        raise HTTPException(status_code=404, detail="Parent user turn not found")

    # 4. Run stream again based on the user turn's parameters
    runner = TurnRunner(session)
    new_turn_create = TurnCreate(
        role="user",
        content=user_turn.content,
        scene_id=user_turn.scene_id,
        parent_turn_id=user_turn.parent_turn_id,
        model_name=user_turn.model_name
    )

    async def token_generator():
        # Note: run_turn_stream will create a new user turn record.
        # This is expected because it starts a new branch/attempt.
        # We could also modify the behavior, but creating a new active user turn
        # is clean because the old one is technically duplicated or continued.
        # However, to avoid duplicating user turns, we can link it as alternative.
        # For simplicity, generating a new user + assistant pair is standard for chat.
        async for token in runner.run_turn_stream(campaign_id, new_turn_create):
            yield token
            
    return StreamingResponse(token_generator(), media_type="text/event-stream")
