from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_session
from app.models.character_card import CharacterCardRead, CharacterCardUpdate
from app.models.session_zero import (
    SessionZeroCompleteRequest,
    SessionZeroCompletionRead,
    SessionZeroRead,
    SessionZeroUpdate,
)
from app.providers.llm_provider import LLMProviderError
from app.services.character_card_service import CharacterCardService
from app.services.session_zero_interview import (
    SessionZeroInterviewIncompleteError,
    SessionZeroInterviewService,
)
from app.services.session_zero_service import (
    SessionZeroIncompleteError,
    SessionZeroLockedError,
    SessionZeroService,
)


router = APIRouter(tags=["session-zero"])


class SessionZeroInterviewAnswerRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)


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


@router.get("/api/campaigns/{campaign_id}/session-zero/interview")
async def get_session_zero_interview(
    campaign_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    """Return the same persisted conversational state that the CLI consumes."""
    try:
        setup = await SessionZeroService(session).get(campaign_id)
        interview = SessionZeroInterviewService(session)
        state = await interview.get_state(campaign_id)
        return {
            "opening_message": interview.OPENING_MESSAGE,
            "status": setup.status,
            "summary": state.last_summary or interview.summary(state.draft),
            "state": state.model_dump(mode="json"),
        }
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


async def _run_interview_turn(
    campaign_id: UUID,
    interview: SessionZeroInterviewService,
    *,
    message: str | None = None,
    retry: bool = False,
):
    try:
        if retry:
            decision = await interview.retry_pending(campaign_id)
            if decision is None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "nothing_to_retry",
                        "message": "Нет сохранённого ответа, ожидающего обработки.",
                    },
                )
        else:
            decision = await interview.answer(campaign_id, message or "")

        completed = False
        scene_title = None
        if decision.ready_to_finalize:
            completion = await interview.finalize(campaign_id)
            completed = True
            scene_title = completion.scene.title

        state = await interview.get_state(campaign_id)
        return {
            "decision": decision.model_dump(mode="json"),
            "completed": completed,
            "scene_title": scene_title,
            "summary": state.last_summary or interview.summary(state.draft),
            "state": state.model_dump(mode="json"),
        }
    except SessionZeroInterviewIncompleteError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "session_zero_interview_incomplete",
                "message": str(exc),
                "missing_fields": exc.missing_fields,
            },
        ) from exc
    except LLMProviderError as exc:
        # Exactly like CLI: the player's answer is already persisted and can be retried.
        rate_limited = interview.is_rate_limited_error(exc)
        public_message = (
            "Провайдер временно отклонил запрос из-за лимита. Твой ответ сохранён. "
            "Подожди немного и повтори запрос."
            if rate_limited
            else (
                "Модель не смогла обработать запрос. Твой ответ сохранён. "
                "Можно повторить его без потери разговора."
            )
        )
        raise HTTPException(
            status_code=502,
            detail={
                "code": "session_zero_provider_error",
                "message": public_message,
                "technical_detail": " ".join(str(exc).split())[:2000],
                "rate_limited": rate_limited,
                "retryable": True,
            },
        ) from exc
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/campaigns/{campaign_id}/session-zero/interview/answer")
async def answer_session_zero_interview(
    campaign_id: UUID,
    request: SessionZeroInterviewAnswerRequest,
    session: AsyncSession = Depends(get_session),
):
    return await _run_interview_turn(
        campaign_id,
        SessionZeroInterviewService(session),
        message=request.message,
    )


@router.post("/api/campaigns/{campaign_id}/session-zero/interview/retry")
async def retry_session_zero_interview(
    campaign_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    return await _run_interview_turn(
        campaign_id,
        SessionZeroInterviewService(session),
        retry=True,
    )


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
