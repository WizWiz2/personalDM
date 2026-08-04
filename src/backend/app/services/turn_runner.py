from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.turn import TurnCreate
from app.services.base_turn_runner import TurnRunner as BaseTurnRunner
from app.services.base_turn_runner import active_tasks
from app.services.narration_pipeline import NarrationPipelineProvider


class TurnRunner(BaseTurnRunner):
    """Public turn orchestrator with an explicit narrator generation pipeline."""

    def __init__(self, session: AsyncSession):
        super().__init__(session)
        self._llm_provider = NarrationPipelineProvider(session)

    async def run_turn_stream(
        self,
        campaign_id: UUID,
        turn_create: TurnCreate,
        existing_user_turn_id: UUID | None = None,
    ) -> AsyncIterator[str]:
        async with self._llm_provider.bind(
            campaign_id,
            existing_user_turn_id,
        ):
            async for item in super().run_turn_stream(
                campaign_id,
                turn_create,
                existing_user_turn_id,
            ):
                yield item


__all__ = ["TurnRunner", "active_tasks"]
