# ruff: noqa: I001

from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_session
from app.db.repositories.job_repo import PostTurnJobRepository
from app.models.memory_ops import MemoryMaintenanceRequest
from app.services.debugger_service import DebuggerService
from app.services.memory_operations import MemoryOperationsService
from app.services.post_turn_processor import PostTurnProcessor
from app.services.presence_debugger import PresenceDebugger
from app.services.scene_transition_debugger import SceneTransitionDebugger
from app.services.session_zero_debugger import SessionZeroDebugger


router = APIRouter(prefix="/api", tags=["debugger"])


@router.get("/debugger", response_class=HTMLResponse, include_in_schema=False)
async def debugger_page():
    path = Path(__file__).resolve().parent.parent / "static" / "debugger.html"
    return HTMLResponse(path.read_text(encoding="utf-8"))


@router.get("/memory-ops", response_class=HTMLResponse, include_in_schema=False)
async def memory_operations_page():
    path = Path(__file__).resolve().parent.parent / "static" / "memory_ops.html"
    return HTMLResponse(path.read_text(encoding="utf-8"))


@router.get("/campaigns/{campaign_id}/debugger")
async def campaign_debugger(
    campaign_id: UUID,
    turn_limit: int = Query(default=100, ge=1, le=1000),
    session: AsyncSession = Depends(get_session),
):
    try:
        snapshot = await DebuggerService(session).snapshot(campaign_id, turn_limit)
        snapshot.update(
            await SceneTransitionDebugger(session).snapshot(
                campaign_id,
                limit=turn_limit,
            )
        )
        presence = await PresenceDebugger(session).snapshot(campaign_id)
        presence_health = presence.pop("health", {})
        snapshot.update(presence)
        snapshot.setdefault("health", {}).update(presence_health)

        setup = await SessionZeroDebugger(session).snapshot(campaign_id)
        setup_health = setup.pop("health", {})
        snapshot.update(setup)
        snapshot.setdefault("health", {}).update(setup_health)

        memory_ops = await MemoryOperationsService(session).snapshot(campaign_id)
        memory_health = memory_ops.pop("health", {})
        snapshot["memory_ops"] = memory_ops
        snapshot.setdefault("health", {}).update(memory_health)
        return snapshot
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/campaigns/{campaign_id}/memory-ops")
async def campaign_memory_operations(
    campaign_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    try:
        return await MemoryOperationsService(session).snapshot(campaign_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/campaigns/{campaign_id}/memory-ops/maintenance")
async def maintain_campaign_memory(
    campaign_id: UUID,
    request: MemoryMaintenanceRequest,
    session: AsyncSession = Depends(get_session),
):
    try:
        result = await MemoryOperationsService(session).maintain(campaign_id, request)
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if request.apply_changes:
        await session.commit()
    else:
        await session.rollback()
    return result


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
