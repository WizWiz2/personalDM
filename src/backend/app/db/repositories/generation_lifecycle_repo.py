from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select

from app.db.generation_lifecycle_table import GenerationLifecycle
from app.db.repositories.base import BaseRepository
from app.db.tables import GenerationRun
from app.models.jobs import GenerationLifecycleRead, GenerationPhase


_PHASE_TIMESTAMPS = {
    GenerationPhase.RECEIVED: "received_at",
    GenerationPhase.PLANNED: "planned_at",
    GenerationPhase.PREPARED: "prepared_at",
    GenerationPhase.NARRATED: "narrated_at",
    GenerationPhase.PUBLISHED: "published_at",
    GenerationPhase.POST_TURN_DONE: "post_turn_done_at",
    GenerationPhase.COMPENSATED: "compensated_at",
}


class GenerationLifecycleRepository(BaseRepository):
    async def start_attempt(self, run_id: UUID) -> GenerationLifecycleRead:
        row = await self._session.get(GenerationLifecycle, str(run_id))
        now = datetime.utcnow()
        if row is None:
            row = GenerationLifecycle(
                generation_run_id=str(run_id),
                phase=GenerationPhase.RECEIVED.value,
                attempt=1,
                received_at=now,
                updated_at=now,
            )
            self._session.add(row)
        else:
            row.attempt += 1
            row.phase = GenerationPhase.RECEIVED.value
            row.received_at = now
            row.planned_at = None
            row.prepared_at = None
            row.narrated_at = None
            row.published_at = None
            row.post_turn_done_at = None
            row.compensated_at = None
            row.updated_at = now
        await self._session.flush()
        return GenerationLifecycleRead.model_validate(row)

    async def get(self, run_id: UUID) -> GenerationLifecycleRead | None:
        row = await self._session.get(GenerationLifecycle, str(run_id))
        return GenerationLifecycleRead.model_validate(row) if row else None

    async def set_phase(
        self,
        run_id: UUID,
        phase: GenerationPhase,
    ) -> GenerationLifecycleRead:
        row = await self._session.get(GenerationLifecycle, str(run_id))
        if row is None:
            raise ValueError(f"Generation lifecycle does not exist for run {run_id}")
        now = datetime.utcnow()
        row.phase = phase.value
        setattr(row, _PHASE_TIMESTAMPS[phase], now)
        row.updated_at = now
        await self._session.flush()
        return GenerationLifecycleRead.model_validate(row)

    async def list_incomplete(
        self,
        campaign_id: UUID | None = None,
    ) -> list[GenerationLifecycleRead]:
        query = (
            select(GenerationLifecycle)
            .join(
                GenerationRun,
                GenerationRun.id == GenerationLifecycle.generation_run_id,
            )
            .where(
                GenerationRun.status.in_(("running", "failed", "cancelled")),
                GenerationLifecycle.phase.in_(
                    (
                        GenerationPhase.PREPARED.value,
                        GenerationPhase.NARRATED.value,
                    )
                ),
            )
            .order_by(GenerationLifecycle.updated_at.asc())
        )
        if campaign_id is not None:
            query = query.where(GenerationRun.campaign_id == str(campaign_id))
        result = await self._session.execute(query)
        return [
            GenerationLifecycleRead.model_validate(row)
            for row in result.scalars().all()
        ]
