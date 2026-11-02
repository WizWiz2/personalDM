"""Recover durable generation work left behind by a dead process.

A running row may belong to another live process using the same SQLite file.
Only the owner of the campaign process lease may recover it. Recovery is idempotent and never blocks boot: each run is
recovered in its own transaction and a bad record is logged and skipped.
"""
from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.generation_lifecycle_table import GenerationLifecycle
from app.db.narration_validation_table import NarrationValidationRun
from app.db.repositories.job_repo import PostTurnJobRepository
from app.db.tables import GenerationRun, Turn
from app.models.jobs import GenerationPhase
from app.services.prepared_turn_compensation import compensate_prepared_turn
from app.services.campaign_process_lease import GenerationAlreadyRunningError, campaign_process_lease

logger = logging.getLogger(__name__)

_INTERRUPTED = "Generation interrupted by previous server shutdown; retry this player turn"
_PREPARED_PHASES = {GenerationPhase.PREPARED.value, GenerationPhase.NARRATED.value}


async def _recover_run(session: AsyncSession, run: GenerationRun) -> None:
    assistant = await session.get(Turn, run.assistant_turn_id) if run.assistant_turn_id else None
    if assistant is not None and assistant.status == "active":
        run.status = "completed"
        return
    lifecycle = await session.get(GenerationLifecycle, run.id)
    if lifecycle is not None and lifecycle.phase in _PREPARED_PHASES:
        run_id = run.id
        await compensate_prepared_turn(session, UUID(run_id), UUID(run.user_turn_id), UUID(run.campaign_id))
        run = await session.get(GenerationRun, run_id)
    run.status = "failed"
    run.error = _INTERRUPTED
    user = await session.get(Turn, run.user_turn_id)
    if user is not None and user.role in {"user", "meta_user"} and user.status == "active":
        user.status = "failed"


async def recover_interrupted_generations(session: AsyncSession) -> int:
    """Close every interrupted run with a typed outcome; return how many were recovered."""
    run_ids = (await session.execute(
        select(GenerationRun.id).where(GenerationRun.status == "running")
        .order_by(GenerationRun.created_at)
    )).scalars().all()
    recovered = 0
    for run_id in run_ids:
        try:
            run = await session.get(GenerationRun, run_id)
            with campaign_process_lease(session.bind, run.campaign_id):
                # Refresh after admission: a prior owner may have finished while we read.
                await session.refresh(run)
                if run.status != "running":
                    continue
                await _recover_run(session, run)
                await session.commit()
                recovered += 1
        except GenerationAlreadyRunningError:
            await session.rollback()
        except Exception:
            await session.rollback()
            logger.exception("Startup recovery skipped generation run %s", run_id)
    try:
        await session.execute(update(NarrationValidationRun).where(
            NarrationValidationRun.status == "validating",
            NarrationValidationRun.trigger_turn_id.in_(select(GenerationRun.user_turn_id).where(
                GenerationRun.status.in_(("failed", "cancelled")))),
        ).values(status="failed", failure_reason="Generation ended before validation completed"))
        # Another server may still own fresh post-turn jobs; respect their leases.
        await PostTurnJobRepository(session).recover_stale()
        await session.commit()
    except Exception:
        await session.rollback()
        logger.exception("Startup recovery could not requeue post-turn jobs")
    if recovered:
        logger.warning("Recovered %s interrupted generation runs", recovered)
    return recovered


__all__ = ["recover_interrupted_generations"]
