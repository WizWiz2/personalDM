from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

from app.config import settings
from app.db.repositories.entity_repo import EntityRepository
from app.models.turn import ChatMessage, TurnCreate
from app.services.base_turn_runner import active_tasks
from app.services.meta_command_router import MetaCommandRunner, parse_meta_command
from app.services.post_turn_dispatcher import PostTurnDispatcher
from app.services.turn_saga import TurnSaga


class TurnRunner(TurnSaga):
    """Public turn orchestrator backed by the explicit inter-agent Turn Saga.

    UI `/talk` historically stored the selected addressee in `acting_character_id`. That legacy
    transport field is converted into durable input routing before the turn enters the Saga. The
    human-controlled player remains the actor of every user turn; the selected NPC is only the
    addressee. Planner must therefore see normal player context plus an explicit addressee contract,
    never an actor-output prompt for the NPC. TurnAuthority chooses the response actor only after the
    structured plan has been resolved.
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
                "user_actor": "player_character",
            }
        )
        snapshot["input_routing"] = routing
        return turn_create.model_copy(
            update={
                "acting_character_id": None,
                "context_snapshot": snapshot,
            }
        )

    async def _inject_planner_addressee_contract(
        self,
        messages: list[ChatMessage],
        metadata: dict,
        addressed_character_id: UUID | None,
    ) -> tuple[list[ChatMessage], dict]:
        if addressed_character_id is None or not messages:
            return messages, metadata

        character = await EntityRepository(self._session).get_character(addressed_character_id)
        addressed_name = character.canonical_name if character else str(addressed_character_id)
        first, *rest = messages
        contract = (
            "[INPUT ROUTING — authoritative]\n"
            "The latest user message always belongs to the human-controlled player character.\n"
            f"Addressed character: {addressed_name} ({addressed_character_id}).\n"
            "This character is the listener/target of the player's message, NOT the speaker or "
            "source of the user action. Plan the human player's intent and actions. Never rewrite "
            "the player intent with the addressed character as its subject, and never introduce "
            "the player character as a new NPC. If the addressed character is still present after "
            "structured execution, TurnAuthority may later assign that character as response actor.\n"
        )
        audited = dict(metadata)
        routing = dict(audited.get("input_routing") or {})
        routing.update(
            {
                "addressed_character_id": str(addressed_character_id),
                "addressed_character_name": addressed_name,
                "user_actor": "player_character",
            }
        )
        audited["input_routing"] = routing
        return [
            ChatMessage(role=first.role, content=f"{first.content}\n\n{contract}"),
            *rest,
        ], audited

    async def _compile(self, campaign_id, turn_create, scene_id, primary_config):
        """Compile Planner context with the player as actor and NPC only as addressee."""
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
        # Critical invariant: addressed NPC context must never activate ACTOR OUTPUT CONTRACT for
        # Planner. That contract means "write as this NPC" and caused Round 26 to invert speaker and
        # addressee. Planner receives ordinary player context plus a narrow routing note instead.
        messages, metadata = await compiler.compile_context(
            campaign_id=campaign_id,
            acting_character_id=None,
            scene_id=scene_id,
            current_user_content=turn_create.content,
            max_budget_override=max_budget_override,
        )
        messages, metadata = await self._inject_planner_addressee_contract(
            messages,
            metadata,
            addressed_id,
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
