from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

from app.models.turn import TurnCreate
from app.services.base_turn_runner import active_tasks
from app.services.post_turn_dispatcher import PostTurnDispatcher
from app.services.turn_saga import TurnSaga


class TurnRunner(TurnSaga):
    """Public turn orchestrator backed by the explicit inter-agent Turn Saga."""

    @staticmethod
    def _requires_fresh_post_turn_memory(turn_create: TurnCreate) -> bool:
        """Consumers that own proposal resolution must wait for proposal extraction.

        Normal gameplay never sets this marker and therefore returns immediately after
        narrative commit. Autonomous simulation does set it because it intentionally
        inspects/resolves proposals before evaluating the next phase.
        """
        snapshot = turn_create.context_snapshot
        return isinstance(snapshot, dict) and isinstance(snapshot.get("simulation"), dict)

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
            # The new Saga intentionally does not spend a minute repeating identical
            # transport failures. Keep the old diagnostic prefix for UI/tests while
            # making the reduced retry budget explicit rather than pretending retries ran.
            if item.startswith("\n[Generation failed:"):
                item = item.replace(
                    "\n[Generation failed:",
                    "\n[Generation failed after retry budget exhausted (1 attempt):",
                    1,
                )
            yield item
        if (
            PostTurnDispatcher.wait_inline_for_tests
            or self._requires_fresh_post_turn_memory(turn_create)
        ):
            await PostTurnDispatcher.wait_for_idle()


__all__ = ["TurnRunner", "active_tasks"]
