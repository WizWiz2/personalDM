from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_session
from app.db.repositories.job_repo import PostTurnJobRepository
from app.services.debugger_service import DebuggerService
from app.services.post_turn_processor import PostTurnProcessor


router = APIRouter(prefix="/api", tags=["debugger"])


@router.get("/debugger", response_class=HTMLResponse, include_in_schema=False)
async def debugger_page():
    path = Path(__file__).resolve().parent.parent / "static" / "debugger.html"
    return HTMLResponse(path.read_text(encoding="utf-8"))


@router.get("/campaigns/{campaign_id}/debugger")
async def campaign_debugger(
    campaign_id: UUID,
    turn_limit: int = Query(default=100, ge=1, le=1000),
    session: AsyncSession = Depends(get_session),
):
    try:
        return await DebuggerService(session).snapshot(campaign_id, turn_limit)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/post-turn-jobs/{job_id}/retry")
async def retry_post_turn_job(
    job_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    job = await PostTurnJobRepository(session).retry(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Post-turn job not found")
    await session.commit()
    try:
        await PostTurnProcessor(session).process_job(job.id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"success": True, "job_id": str(job.id)}


@router.post("/turns/{assistant_turn_id}/post-turn/process")
async def process_post_turn(
    assistant_turn_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    await PostTurnProcessor(session).process_turn(assistant_turn_id)
    return {"success": True, "assistant_turn_id": str(assistant_turn_id)}
