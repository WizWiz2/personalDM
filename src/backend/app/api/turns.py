import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_session
from app.db.repositories.turn_repo import TurnRepository
from app.db.tables import Turn
from app.models.turn import TurnCreate, TurnRead
from app.services.memory_scribe_guard import install as install_memory_scribe_guard
from app.services.turn_runner import TurnRunner


install_memory_scribe_guard()

router = APIRouter(prefix="/api/campaigns/{campaign_id}/turns", tags=["turns"])


@router.post("", response_class=StreamingResponse)
async def send_turn(
    campaign_id: UUID,
    data: TurnCreate,
    session: AsyncSession = Depends(get_session),
):
    if data.role != "user":
        raise HTTPException(
            status_code=400,
            detail="The public turn endpoint accepts only role='user'",
        )

    runner = TurnRunner(session)

    async def token_generator():
        async for token in runner.run_turn_stream(campaign_id, data):
            yield token

    return StreamingResponse(
        token_generator(),
        media_type="text/plain; charset=utf-8",
    )


@router.post("/stop")
async def stop_generation(
    campaign_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    return {
        "success": await TurnRunner.stop_generation(campaign_id, session)
    }


@router.get("", response_model=list[TurnRead])
async def get_history(
    campaign_id: UUID,
    limit: int = 50,
    active_only: bool = True,
    session: AsyncSession = Depends(get_session),
):
    return await TurnRepository(session).get_history(
        campaign_id,
        limit,
        active_only,
    )


@router.post("/undo")
async def undo_last_pair(
    campaign_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    repo = TurnRepository(session)
    success = await repo.undo_last_pair(campaign_id)
    if not success:
        raise HTTPException(
            status_code=400,
            detail="The latest active turns are not a user/assistant pair",
        )
    await session.commit()
    return {"success": True}


@router.post("/{turn_id}/regenerate", response_class=StreamingResponse)
async def regenerate_turn(
    campaign_id: UUID,
    turn_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(Turn).where(
            Turn.id == str(turn_id),
            Turn.campaign_id == str(campaign_id),
        )
    )
    db_assistant = result.scalar_one_or_none()
    if not db_assistant or db_assistant.role != "assistant":
        raise HTTPException(
            status_code=404,
            detail="Assistant turn to regenerate not found",
        )
    if not db_assistant.parent_turn_id:
        raise HTTPException(
            status_code=400,
            detail="Cannot regenerate a turn without a parent user turn",
        )

    repo = TurnRepository(session)
    user_turn = await repo.get_by_id(UUID(db_assistant.parent_turn_id))
    if not user_turn or user_turn.campaign_id != campaign_id:
        raise HTTPException(status_code=404, detail="Parent user turn not found")

    actor_id = None
    if db_assistant.context_snapshot:
        try:
            snapshot = json.loads(db_assistant.context_snapshot)
            actor_value = snapshot.get("acting_character_id")
            if actor_value:
                actor_id = UUID(actor_value)
        except (json.JSONDecodeError, ValueError, TypeError):
            actor_id = None

    await repo.mark_alternative(turn_id)
    await session.commit()

    runner = TurnRunner(session)
    regeneration_input = TurnCreate(
        role="user",
        content=user_turn.content,
        scene_id=user_turn.scene_id,
        acting_character_id=actor_id,
        parent_turn_id=user_turn.parent_turn_id,
        model_name=user_turn.model_name,
    )

    async def token_generator():
        async for token in runner.run_turn_stream(
            campaign_id,
            regeneration_input,
            existing_user_turn_id=user_turn.id,
        ):
            yield token

    return StreamingResponse(
        token_generator(),
        media_type="text/plain; charset=utf-8",
    )
