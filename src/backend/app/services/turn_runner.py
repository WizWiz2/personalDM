from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

from app.models.turn import TurnCreate
from app.services.base_turn_runner import active_tasks
from app.services.meta_command_router import MetaCommandRunner, parse_meta_command
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
        # GameApplication is the normal routing boundary, but test harnesses and local agents have
        # historically called TurnRunner directly. A meta command must never become a narrative
        # player action merely because a caller bypassed that application facade.
        command = parse_meta_command(turn_create.content)
        if command is not None:
            if existing_user_turn_id is not None:
                raise ValueError(
                    "Meta command reached TurnRunner after narrative persistence; route it through GameApplication"
                )
            async for item in MetaCommandRunner(self._session).run_stream(
                campaign_id,
                command,
            ):
                yield item
            return

        async for item in super().run_turn_stream(
            campaign_id,
            turn_create,
            existing_user_turn_id,
        ):
            # Keep the established CLI/test prefix for true generation failures. Semantic
            # narration violations are recovered inside AuthorityNarrationPipeline and no longer
            # reach this branch at all.
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
