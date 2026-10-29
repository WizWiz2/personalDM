"""Recover durable generation work left behind by a dead process.

Runs before the server accepts requests, so every ``running`` row belongs to a
dead process. Recovery is idempotent and never blocks boot: each run is
recovered in its own transaction and a bad record is logged and skipped.
"""
from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.generation_lifecycle_table import GenerationLifecycle
from app.db.narration_validation_table import NarrationValidationRun
from app.db.repositories.generation_lifecycle_repo import GenerationLifecycleRepository
from app.db.repositories.job_repo import PostTurnJobRepository
from app.db.scene_transition_table import SceneTransition
from app.db.tables import GenerationRun, Turn
from app.models.jobs import GenerationPhase

logger = logging.getLogger(__name__)

_INTERRUPTED = "Generation interrupted by previous server shutdown; retry this player turn"
_PREPARED_PHASES = {GenerationPhase.PREPARED.value, GenerationPhase.NARRATED.value}


async def _compensate_prepared(session: AsyncSession, run: GenerationRun) -> None:
    """Saga compensation from durable records: roll back prepared transitions, mark COMPENSATED."""
    from app.services.master_service import MasterService
    from app.services.scene_transition_executor import SceneTransitionExecutor

    executor = SceneTransitionExecutor(session)
    transition_ids = (await session.execute(select(SceneTransition.id).where(
        SceneTransition.trigger_turn_id == run.user_turn_id,
        SceneTransition.status == "prepared",
    ))).scalars().all()
    for transition_id in transition_ids:
        if not await executor.rollback_transition(UUID(transition_id)):
            raise RuntimeError(f"prepared transition {transition_id} could not be rolled back")
    await MasterService(session).rewind_pending_rhythm(UUID(run.campaign_id))
    await GenerationLifecycleRepository(session).set_phase(UUID(run.id), GenerationPhase.COMPENSATED)


async def _recover_run(session: AsyncSession, run: GenerationRun) -> None:
    assistant = await session.get(Turn, run.assistant_turn_id) if run.assistant_turn_id else None
    if assistant is not None and assistant.status == "active":
        run.status = "completed"
        return
    lifecycle = await session.get(GenerationLifecycle, run.id)
    if lifecycle is not None and lifecycle.phase in _PREPARED_PHASES:
        await _compensate_prepared(session, run)
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
            await _recover_run(session, await session.get(GenerationRun, run_id))
            await session.commit()
            recovered += 1
        except Exception:
            await session.rollback()
            logger.exception("Startup recovery skipped generation run %s", run_id)
    try:
        await session.execute(update(NarrationValidationRun).where(
            NarrationValidationRun.status == "validating",
            NarrationValidationRun.trigger_turn_id.in_(select(GenerationRun.user_turn_id).where(
                GenerationRun.status.in_(("failed", "cancelled")))),
        ).values(status="failed", failure_reason="Generation ended before validation completed"))
        # The previous process is dead: every running post-turn job is stale.
        await PostTurnJobRepository(session).recover_stale(stale_after_seconds=0)
        await session.commit()
    except Exception:
        await session.rollback()
        logger.exception("Startup recovery could not requeue post-turn jobs")
    if recovered:
        logger.warning("Recovered %s interrupted generation runs", recovered)
    return recovered


__all__ = ["recover_interrupted_generations"]
