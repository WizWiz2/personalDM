from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_session
from app.models.character_card import CharacterCardRead, CharacterCardUpdate
from app.models.session_zero import (
    SessionZeroCompleteRequest,
    SessionZeroCompletionRead,
    SessionZeroRead,
    SessionZeroUpdate,
)
from app.services.character_card_service import CharacterCardService
from app.services.session_zero_service import (
    SessionZeroIncompleteError,
    SessionZeroLockedError,
    SessionZeroService,
)


router = APIRouter(tags=["session-zero"])


@router.get(
    "/api/session-zero-ui",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def session_zero_page():
    path = Path(__file__).resolve().parent.parent / "static" / "session_zero.html"
    return HTMLResponse(path.read_text(encoding="utf-8"))


@router.get(
    "/api/campaigns/{campaign_id}/session-zero",
    response_model=SessionZeroRead,
)
async def get_session_zero(
    campaign_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    try:
        return await SessionZeroService(session).get(campaign_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put(
    "/api/campaigns/{campaign_id}/session-zero",
    response_model=SessionZeroRead,
)
async def update_session_zero(
    campaign_id: UUID,
    data: SessionZeroUpdate,
    session: AsyncSession = Depends(get_session),
):
    try:
        result = await SessionZeroService(session).update(campaign_id, data)
        await session.commit()
        return result
    except SessionZeroLockedError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail={"code": "session_zero_locked", "message": str(exc)},
        ) from exc
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/api/campaigns/{campaign_id}/session-zero/complete",
    response_model=SessionZeroCompletionRead,
)
async def complete_session_zero(
    campaign_id: UUID,
    request: SessionZeroCompleteRequest | None = None,
    session: AsyncSession = Depends(get_session),
):
    try:
        result = await SessionZeroService(session).complete(campaign_id, request)
        await session.commit()
        return result
    except SessionZeroIncompleteError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "session_zero_incomplete",
                "message": str(exc),
                "missing_fields": exc.missing_fields,
            },
        ) from exc
    except (SessionZeroLockedError, ValueError) as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/api/characters/{character_id}/card",
    response_model=CharacterCardRead,
)
async def get_character_card(
    character_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    try:
        return await CharacterCardService(session).get_card(character_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put(
    "/api/characters/{character_id}/card",
    response_model=CharacterCardRead,
)
async def update_character_card(
    character_id: UUID,
    data: CharacterCardUpdate,
    session: AsyncSession = Depends(get_session),
):
    try:
        result = await CharacterCardService(session).update_card(
            character_id,
            data,
        )
        await session.commit()
        return result
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
