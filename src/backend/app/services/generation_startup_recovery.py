"""Recover durable, pre-publication player inputs after process termination.

Only RECEIVED/PLANNED phases are safe to reclassify automatically: PREPARED
and NARRATED may have committed world changes and need compensation first.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.generation_lifecycle_table import GenerationLifecycle
from app.db.tables import GenerationRun, Turn

logger = logging.getLogger(__name__)


async def recover_interrupted_generations(session: AsyncSession) -> int:
    """Make uncommitted interrupted player inputs explicitly retryable.

    Runs only before the server accepts requests, never while an in-memory
    generation may still be running. No world state is modified.
    """
    rows = (await session.execute(
        select(GenerationRun).where(GenerationRun.status == "running")
        .order_by(GenerationRun.created_at)
    )).scalars().all()
    # Fail closed BEFORE changing anything if an interrupted run had committed
    # prepared world mutations. Silently retrying or marking it failed would
    # potentially leave the scene halfway through a turn.
    for run in rows:
        lifecycle = await session.get(GenerationLifecycle, run.id)
        phase = lifecycle.phase if lifecycle is not None else "received"
        if phase in {"prepared", "narrated"}:
            raise RuntimeError(
                f"Unsafe interrupted generation {run.id} in phase={phase}: "
                "recover/compensate the prepared attempt before accepting gameplay."
            )
    recovered = 0
    for run in rows:
        assistant = (
            await session.get(Turn, run.assistant_turn_id)
            if run.assistant_turn_id else None
        )
        if assistant is not None and assistant.status == "active":
            run.status = "completed"
            recovered += 1
            continue
        run.status = "failed"
        run.error = "Generation interrupted by previous server shutdown; retry this player turn"
        user = await session.get(Turn, run.user_turn_id)
        if user is not None and user.role in {"user", "meta_user"} and user.status == "active":
            user.status = "failed"
        recovered += 1
    if recovered:
        await session.commit()
        logger.warning("Recovered %s interrupted generation runs", recovered)
    return recovered


__all__ = ["recover_interrupted_generations"]
