from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

from app.config import settings
from app.models.turn import TurnCreate
from app.services.base_turn_runner import active_tasks
from app.services.meta_command_router import MetaCommandRunner, parse_meta_command
from app.services.post_turn_dispatcher import PostTurnDispatcher
from app.services.turn_saga import TurnSaga


class TurnRunner(TurnSaga):
    """Public turn orchestrator backed by the explicit inter-agent Turn Saga.

    UI `/talk` historically stored the selected addressee in `acting_character_id`. Passing that
    value straight into TurnSaga also disabled Planner, so a message such as "я ухожу; Анна, вы со
    мной?" could never produce structured movement. At this public boundary the selected NPC is now
    recorded as routing context, while the player turn itself enters Saga unscoped and is always
    planned. TurnAuthority later decides whether the addressed NPC is still allowed to own the
    response.
    """

    @staticmethod
    def _requires_fresh_post_turn_memory(turn_create: TurnCreate) -> bool:
        snapshot = turn_create.context_snapshot
        return isinstance(snapshot, dict) and isinstance(snapshot.get("simulation"), dict)

    @staticmethod
    def _addressed_character_id(turn_create: TurnCreate) -> UUID | None:
        snapshot = turn_create.context_snapshot
        if not isinstance(snapshot, dict):
            return None
        routing = snapshot.get("input_routing")
        if not isinstance(routing, dict):
            return None
        value = routing.get("addressed_character_id")
        try:
            return UUID(str(value)) if value else None
        except (TypeError, ValueError):
            return None

    @classmethod
    def _route_addressed_input(cls, turn_create: TurnCreate) -> TurnCreate:
        if turn_create.acting_character_id is None:
            return turn_create
        snapshot = dict(turn_create.context_snapshot or {})
        routing = dict(snapshot.get("input_routing") or {})
        routing.update(
            {
                "addressed_character_id": str(turn_create.acting_character_id),
                "legacy_acting_character_field": True,
                "planner_bypass": False,
            }
        )
        snapshot["input_routing"] = routing
        return turn_create.model_copy(
            update={
                "acting_character_id": None,
                "context_snapshot": snapshot,
            }
        )

    async def _compile(self, campaign_id, turn_create, scene_id, primary_config):
        """Compile actor-aware context while still reserving budget for Planner."""
        from app.services.context_compiler import ContextCompiler

        addressed_id = self._addressed_character_id(turn_create)
        safety_margin = int(primary_config.context_window * settings.SAFETY_MARGIN_PERCENT)
        max_budget_override = max(
            512,
            primary_config.context_window
            - settings.RESPONSE_RESERVE_TOKENS
            - safety_margin
            - settings.PLANNER_CONTEXT_RESERVE_TOKENS,
        )
        compiler = ContextCompiler(self._session)
        messages, metadata = await compiler.compile_context(
            campaign_id=campaign_id,
            acting_character_id=addressed_id,
            scene_id=scene_id,
            current_user_content=turn_create.content,
            max_budget_override=max_budget_override,
        )
        return (
            self._reserve_current_user(messages, metadata, turn_create.content),
            compiler,
            max_budget_override,
        )

    async def _recompile_narrator_context(
        self,
        *,
        compiler,
        campaign_id,
        turn_create,
        scene_id,
        max_budget_override,
    ):
        addressed_id = self._addressed_character_id(turn_create)
        messages, metadata = await compiler.compile_context(
            campaign_id=campaign_id,
            acting_character_id=addressed_id,
            scene_id=scene_id,
            current_user_content=turn_create.content,
            max_budget_override=max_budget_override,
        )
        return self._reserve_current_user(messages, metadata, turn_create.content)

    async def run_turn_stream(
        self,
        campaign_id: UUID,
        turn_create: TurnCreate,
        existing_user_turn_id: UUID | None = None,
    ) -> AsyncIterator[str]:
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

        routed_turn = self._route_addressed_input(turn_create)
        async for item in super().run_turn_stream(
            campaign_id,
            routed_turn,
            existing_user_turn_id,
        ):
            if item.startswith("\n[Generation failed:"):
                item = item.replace(
                    "\n[Generation failed:",
                    "\n[Generation failed after retry budget exhausted (1 attempt):",
                    1,
                )
            yield item
        if (
            PostTurnDispatcher.wait_inline_for_tests
            or self._requires_fresh_post_turn_memory(routed_turn)
        ):
            await PostTurnDispatcher.wait_for_idle()


__all__ = ["TurnRunner", "active_tasks"]
