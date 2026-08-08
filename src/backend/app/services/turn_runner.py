from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

from app.models.turn import TurnCreate
from app.services.base_turn_runner import active_tasks
from app.services.post_turn_dispatcher import PostTurnDispatcher
from app.services.turn_saga import TurnSaga


class TurnRunner(TurnSaga):
    """Public turn orchestrator backed by the explicit inter-agent Turn Saga."""

    async def run_turn_stream(
        self,
        campaign_id: UUID,
        turn_create: TurnCreate,
        existing_user_turn_id: UUID | None = None,
    ) -> AsyncIterator[str]:
        async for item in super().run_turn_stream(
            campaign_id,
            turn_create,
            existing_user_turn_id,
        ):
            yield item
        if PostTurnDispatcher.wait_inline_for_tests:
            await PostTurnDispatcher.wait_for_idle()


__all__ = ["TurnRunner", "active_tasks"]
