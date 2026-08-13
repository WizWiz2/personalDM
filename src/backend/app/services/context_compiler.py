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
    """Compile base context, then apply explicit ordered context providers."""

    DEFAULT_PROVIDER_NAMES = (
        SceneStateContextProvider.name,
        NarrativeDetailsContextProvider.name,
    )

    NARRATOR_SURFACE_CONTRACT = """[PLAYER-FACING NARRATION CONTRACT]
Write the first draft in the same player-facing form that can be published without repair.
- If the latest player message is Russian, write the entire in-game response in Russian. Keep only
  established proper names unchanged; never switch to Chinese or English explanatory prose.
- Address the human-controlled protagonist in second person. Do not repeatedly narrate the
  protagonist by canonical name in third person. The player's message already owns every voluntary
  action or line of dialogue; describe only its resolved effect and the world's response.
- Write only in-world prose. Never expose UUIDs, slugs, database/location paths, route diagnostics,
  TURN AUTHORITY fields, BLOCKED/SKIPPED labels, validator language, or phrases about "the player",
  "the response", "the narration", or waiting for the player's next input.
- If an action is structurally blocked, describe only the concrete in-world obstacle or lack of
  progress supported by the prompt. Do not print an engine status or technical rejection reason.
- Do not restate the current input as a summary. Advance from it to the smallest concrete,
  authority-supported consequence and stop before inventing the protagonist's next choice.
"""

    PLAYER_CONTROL_CONTRACT = """[PLAYER-CONTROLLED PROTAGONIST: {player_name}]
{player_name} is controlled exclusively by the human player. The latest user message is the complete
speech/action the human supplied for this turn. You may perceive it, answer it, react to it, or ask a
question back, but never add new dialogue, voluntary movement, gestures, choices, plans, beliefs,
emotions, promises, attacks, consent, or other intentional actions for {player_name}.

[ACTOR OUTPUT CONTRACT: {actor_name}]
Write only {actor_name}'s own speech, actions, perceptions and immediate reactions. Never narrate
{player_name} as the subject of a new action and never write a new quoted line for {player_name}.
End immediately after {actor_name}'s response; the human supplies what {player_name} does next.
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

    @staticmethod
    def _remove_player_from_other_npcs(content: str, player_name: str) -> str:
        """A human-controlled protagonist must never be represented to an NPC as another NPC."""
        lines = content.splitlines()
        result: list[str] = []
        in_other_npcs = False
        skipping_player = False
        player_prefix = f"- {player_name} (Status:"

        for line in lines:
            stripped = line.strip()
            if stripped == "[Other Present NPCs]":
                in_other_npcs = True
                skipping_player = False
                result.append(line)
                continue

            if in_other_npcs and line.startswith(player_prefix):
                skipping_player = True
                continue

            if skipping_player:
                if line.startswith("  "):
                    continue
                skipping_player = False

            if in_other_npcs and stripped.startswith("[") and stripped.endswith("]"):
                in_other_npcs = False

            result.append(line)

        return "\n".join(result)

    async def _apply_narrator_surface_contract(
        self,
        messages: list[ChatMessage],
        metadata: dict,
        acting_character_id: UUID | None,
    ) -> tuple[list[ChatMessage], dict]:
        if acting_character_id is not None or not messages:
            return messages, metadata
        first, *rest = messages
        audited = dict(metadata)
        layers = list(audited.get("included_layers") or [])
        if "layer_0a_narrator_surface" not in layers:
            layers.append("layer_0a_narrator_surface")
        audited["included_layers"] = layers
        return [
            ChatMessage(
                role=first.role,
                content=f"{first.content}\n\n{self.NARRATOR_SURFACE_CONTRACT}",
            ),
            *rest,
        ], audited

    async def _apply_actor_ownership_contract(
        self,
        campaign_id: UUID,
        acting_character_id: UUID | None,
        messages: list[ChatMessage],
        metadata: dict,
    ) -> tuple[list[ChatMessage], dict]:
        if not acting_character_id or not messages:
            return messages, metadata

        campaign = await self._campaign_repo.get_by_id(campaign_id)
        player_id = campaign.player_character_id if campaign else None
        if not player_id or player_id == acting_character_id:
            return messages, metadata
        player = await self._entity_repo.get_character(player_id)
        actor = await self._entity_repo.get_character(acting_character_id)
        if not player or not actor:
            return messages, metadata

        first, *rest = messages
        cleaned = self._remove_player_from_other_npcs(
            first.content,
            player.canonical_name,
        )
        cleaned = (
            f"{cleaned}\n\n"
            + self.PLAYER_CONTROL_CONTRACT.format(
                player_name=player.canonical_name,
                actor_name=actor.canonical_name,
            )
        )
        audited = dict(metadata)
        audited["player_controlled_protagonist_id"] = str(player.id)
        audited["player_controlled_protagonist_name"] = player.canonical_name
        audited["actor_output_character_id"] = str(actor.id)
        audited["actor_output_character_name"] = actor.canonical_name
        audited["included_character_ids"] = [
            value
            for value in list(audited.get("included_character_ids") or [])
            if value != str(player.id)
        ]
        layers = list(audited.get("included_layers") or [])
        if "layer_0b_player_ownership" not in layers:
            layers.append("layer_0b_player_ownership")
        if "layer_0c_actor_output_contract" not in layers:
            layers.append("layer_0c_actor_output_contract")
        audited["included_layers"] = layers
        return [ChatMessage(role=first.role, content=cleaned), *rest], audited

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
        messages, metadata = await self._apply_narrator_surface_contract(
            messages,
            metadata,
            acting_character_id,
        )
        messages, metadata = await self._apply_actor_ownership_contract(
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
