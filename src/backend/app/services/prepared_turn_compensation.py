"""One compensation path for a PREPARED turn, used by the saga and by startup recovery.

Everything it undoes is read from durable records keyed by the player turn, so it works the
same in the failing process and after a crash, and running it twice changes nothing.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.generation_lifecycle_repo import GenerationLifecycleRepository
from app.db.scene_transition_table import SceneTransition
from app.models.jobs import GenerationPhase


async def compensate_prepared_turn(
    session: AsyncSession, run_id: UUID, user_turn_id: UUID, campaign_id: UUID,
) -> None:
    from app.services.master_service import MasterService
    from app.services.scene_transition_executor import SceneTransitionExecutor
    from app.services.turn_outcome_materializer import TurnOutcomeMaterializer

    await session.rollback()  # drop anything after the durable PREPARED boundary
    await TurnOutcomeMaterializer(session).rollback(user_turn_id)
    executor = SceneTransitionExecutor(session)
    for transition_id in (await session.execute(select(SceneTransition.id).where(
        SceneTransition.trigger_turn_id == str(user_turn_id), SceneTransition.status == "prepared",
    ))).scalars().all():
        if not await executor.rollback_transition(UUID(transition_id)):
            raise RuntimeError(f"prepared transition {transition_id} could not be rolled back")
    await MasterService(session).rewind_pending_rhythm(campaign_id)
    await GenerationLifecycleRepository(session).set_phase(run_id, GenerationPhase.COMPENSATED)
    await session.commit()


__all__ = ["compensate_prepared_turn"]
