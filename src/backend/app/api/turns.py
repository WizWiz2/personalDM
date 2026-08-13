from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
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
from app.db.repositories.job_repo import GenerationRunRepository
from app.db.repositories.turn_repo import TurnRepository
from app.models.jobs import GenerationRunRead
from app.models.turn import TurnCreate, TurnRead
from app.services.detached_turn_dispatcher import (
    DetachedTurnDispatcher,
    GenerationAlreadyRunningError,
)

router = APIRouter(prefix="/api/campaigns/{campaign_id}/turns", tags=["turns"])


def _raise_input_error(campaign_id: UUID, exc: Exception) -> None:
    if isinstance(exc, CampaignNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, GameNotReadyError):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "session_zero_required",
                "message": "Complete session zero before starting narrative play",
                "missing_fields": exc.missing_fields,
                "session_zero_url": f"/api/campaigns/{campaign_id}/session-zero",
            },
        ) from exc
    if isinstance(exc, (CurrentSceneError, GenerationAlreadyRunningError)):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    raise exc


@router.post("", response_class=StreamingResponse)
async def send_turn(
    campaign_id: UUID,
    data: TurnCreate,
    session: AsyncSession = Depends(get_session),
):
    """Compatibility streaming endpoint used by older clients and tests.

    New GUI clients should use `/async`; its generation is detached from the HTTP
    connection and therefore survives React navigation and other parallel UI work.
    """
    if data.role != "user":
        raise HTTPException(
            status_code=400,
            detail="The public turn endpoint accepts only role='user'",
        )

    try:
        route = await GameApplication(session).route_input(campaign_id, data)
    except (CampaignNotFoundError, GameNotReadyError, CurrentSceneError) as exc:
        _raise_input_error(campaign_id, exc)

    return StreamingResponse(
        route.stream,
        media_type="text/plain; charset=utf-8",
        headers={"X-PersonalDM-Channel": route.channel},
    )


@router.post("/async", status_code=status.HTTP_202_ACCEPTED)
async def send_turn_async(
    campaign_id: UUID,
    data: TurnCreate,
    session: AsyncSession = Depends(get_session),
):
    """Persist the player's input first and resolve it in a server-owned background task."""
    if data.role != "user":
        raise HTTPException(
            status_code=400,
            detail="The public turn endpoint accepts only role='user'",
        )
    try:
        accepted = await DetachedTurnDispatcher.submit(campaign_id, data, session)
    except (
        CampaignNotFoundError,
        GameNotReadyError,
        CurrentSceneError,
        GenerationAlreadyRunningError,
    ) as exc:
        _raise_input_error(campaign_id, exc)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "accepted": True,
        "channel": accepted.channel,
        "user_turn": accepted.user_turn,
        "generation": accepted.generation,
    }


@router.get("/generation/latest", response_model=GenerationRunRead | None)
async def get_latest_generation(
    campaign_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    runs = await GenerationRunRepository(session).list_for_campaign(campaign_id, limit=1)
    return runs[0] if runs else None


@router.post("/stop")
async def stop_generation(
    campaign_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    requested = await GameApplication(session).stop_generation(campaign_id)
    detached = DetachedTurnDispatcher.cancel_task(campaign_id)
    return {"success": bool(requested or detached)}


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
