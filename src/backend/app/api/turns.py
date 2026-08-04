from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.application import (
    CampaignNotFoundError,
    CurrentSceneError,
    GameApplication,
    GameNotReadyError,
    TurnNotFoundError,
    TurnRegenerationError,
)
from app.db.engine import get_session
from app.db.repositories.turn_repo import TurnRepository
from app.models.turn import TurnCreate, TurnRead

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

    try:
        route = await GameApplication(session).route_input(campaign_id, data)
    except CampaignNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except GameNotReadyError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "session_zero_required",
                "message": "Complete session zero before starting narrative play",
                "missing_fields": exc.missing_fields,
                "session_zero_url": f"/api/campaigns/{campaign_id}/session-zero",
            },
        ) from exc
    except CurrentSceneError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return StreamingResponse(
        route.stream,
        media_type="text/plain; charset=utf-8",
        headers={"X-PersonalDM-Channel": route.channel},
    )


@router.post("/stop")
async def stop_generation(
    campaign_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    return {
        "success": await GameApplication(session).stop_generation(campaign_id)
    }


@router.get("", response_model=list[TurnRead])
async def get_history(
    campaign_id: UUID,
    limit: int = 50,
    active_only: bool = True,
    channel: Literal["all", "narrative", "meta"] = "all",
    session: AsyncSession = Depends(get_session),
):
    return await TurnRepository(session).get_history(
        campaign_id,
        limit,
        active_only,
        channel=channel,
    )


@router.post("/undo")
async def undo_last_pair(
    campaign_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    success = await GameApplication(session).undo_last_turn(campaign_id)
    if not success:
        raise HTTPException(
            status_code=400,
            detail="The latest active narrative turns are not a user/assistant pair",
        )
    return {"success": True}


@router.post("/{turn_id}/regenerate", response_class=StreamingResponse)
async def regenerate_turn(
    campaign_id: UUID,
    turn_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    try:
        route = await GameApplication(session).regenerate_turn(campaign_id, turn_id)
    except TurnNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TurnRegenerationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return StreamingResponse(
        route.stream,
        media_type="text/plain; charset=utf-8",
        headers={"X-PersonalDM-Channel": route.channel},
    )
