from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.turn import ChatMessage
from app.services.base_context_compiler import (
    ContextCompiler as BaseContextCompiler,
)
from app.services.base_context_compiler import count_tokens
from app.services.context_pipeline import (
    ContextPipeline,
    ContextProvider,
    ContextRequest,
    NarrativeDetailsContextProvider,
    SceneStateContextProvider,
)


class ContextCompiler(BaseContextCompiler):
    """Compile the base prompt and enrich it through explicit ordered providers."""

    DEFAULT_PROVIDER_NAMES = (
        SceneStateContextProvider.name,
        NarrativeDetailsContextProvider.name,
    )

    NEW_NPC_CONTRACT = """[ENGINE NPC INTRODUCTION CAPABILITY]
The structured participant list remains exhaustive for every character already known to this
campaign: a known absent character cannot silently arrive, speak, act, or be substituted for a
new person. There is one narrow exception for genuinely new NPCs that do not yet exist in campaign
truth. When the player's latest action directly seeks contact with an unspecified person or an
ordinary role naturally available at the current place (for example knocking on an inhabited door,
asking a clerk, guard, bartender, witness, passer-by, or similar incidental person), the planner and
narrator may introduce that previously unknown NPC in the current scene. The introduction must be a
plausible immediate consequence of the player's action, not an unseeded dramatic interruption.
Accepted narrator prose is registered by EntityRegistrar after the turn. Therefore a genuinely new
NPC must not be rejected solely because they were not a participant before this turn. This exception
never permits teleporting or reintroducing a character whose identity/name/alias is already present
in campaign context and currently absent.
"""

    PLAYER_CONTROL_CONTRACT = """[PLAYER-CONTROLLED PROTAGONIST: {player_name}]
{player_name} is controlled exclusively by the human player. The latest user message is the complete
speech/action the human supplied for this turn. You may perceive it, answer it, react to it, or ask a
question back, but never add new dialogue, voluntary movement, gestures, choices, plans, beliefs,
emotions, promises, attacks, consent, or other intentional actions for {player_name}. Stop before
choosing the protagonist's next response. If {player_name} also appears in a generic participant or
"Other Present NPCs" summary, that listing means physical presence only and never grants control of
the protagonist.
"""

    def __init__(
        self,
        session: AsyncSession,
        context_providers: Sequence[ContextProvider] | None = None,
    ):
        super().__init__(session)
        providers = (
            tuple(context_providers)
            if context_providers is not None
            else (
                SceneStateContextProvider(session),
                NarrativeDetailsContextProvider(session, count_tokens),
            )
        )
        self._context_pipeline = ContextPipeline(providers)

    @property
    def context_provider_names(self) -> tuple[str, ...]:
        return self._context_pipeline.provider_names

    async def _inject_engine_contracts(
        self,
        campaign_id: UUID,
        acting_character_id: UUID | None,
        messages: list[ChatMessage],
        metadata: dict,
    ) -> tuple[list[ChatMessage], dict]:
        if not messages:
            return messages, metadata

        blocks: list[str] = []
        audited = dict(metadata)
        if acting_character_id is None:
            blocks.append(self.NEW_NPC_CONTRACT)
            audited["new_npc_introduction_contract"] = True
        else:
            campaign = await self._campaign_repo.get_by_id(campaign_id)
            player_id = campaign.player_character_id if campaign else None
            if player_id and player_id != acting_character_id:
                player = await self._entity_repo.get_character(player_id)
                if player:
                    blocks.append(
                        self.PLAYER_CONTROL_CONTRACT.format(
                            player_name=player.canonical_name,
                        )
                    )
                    audited["player_controlled_protagonist_id"] = str(player.id)
                    audited["player_controlled_protagonist_name"] = (
                        player.canonical_name
                    )

        if not blocks:
            return messages, audited

        first, *rest = messages
        enriched = ChatMessage(
            role=first.role,
            content=f"{first.content}\n\n" + "\n".join(blocks),
        )
        layers = list(audited.get("included_layers") or [])
        if "layer_0b_engine_contracts" not in layers:
            layers.append("layer_0b_engine_contracts")
        audited["included_layers"] = layers
        return [enriched, *rest], audited

    async def compile_context(
        self,
        campaign_id: UUID,
        acting_character_id: UUID | None = None,
        scene_id: UUID | None = None,
        current_user_content: str | None = None,
        max_budget_override: int | None = None,
    ) -> tuple[list[ChatMessage], dict]:
        messages, metadata = await super().compile_context(
            campaign_id=campaign_id,
            acting_character_id=acting_character_id,
            scene_id=scene_id,
            current_user_content=current_user_content,
            max_budget_override=max_budget_override,
        )
        messages, metadata = await self._inject_engine_contracts(
            campaign_id,
            acting_character_id,
            messages,
            metadata,
        )
        return await self._context_pipeline.enrich(
            ContextRequest(
                campaign_id=campaign_id,
                acting_character_id=acting_character_id,
                scene_id=scene_id,
                current_user_content=current_user_content,
                max_budget_override=max_budget_override,
            ),
            messages,
            metadata,
        )


__all__ = ["ContextCompiler", "count_tokens"]
