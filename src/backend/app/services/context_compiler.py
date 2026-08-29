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
from app.services.prompt_policy import CURRENT_PROMPT_POLICY, PromptPolicy


class ContextCompiler(BaseContextCompiler):
    """Compile context data, then layer a versioned prompt policy and ordered providers."""

    DEFAULT_PROVIDER_NAMES = (
        SceneStateContextProvider.name,
        NarrativeDetailsContextProvider.name,
    )

    # Compatibility aliases for callers/tests that inspect these contracts directly. Their source
    # of truth is PromptPolicy, not ContextCompiler.
    NARRATOR_SURFACE_CONTRACT = CURRENT_PROMPT_POLICY.narrator_surface_contract
    PLAYER_CONTROL_CONTRACT = CURRENT_PROMPT_POLICY.player_control_contract

    def __init__(
        self,
        session: AsyncSession,
        context_providers: Sequence[ContextProvider] | None = None,
        prompt_policy: PromptPolicy = CURRENT_PROMPT_POLICY,
    ):
        super().__init__(session)
        self._prompt_policy = prompt_policy
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

    @property
    def prompt_policy_version(self) -> str:
        return self._prompt_policy.version

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

    def _audit_prompt_policy(self, metadata: dict) -> dict:
        audited = dict(metadata)
        audited["prompt_policy_version"] = self._prompt_policy.version
        return audited

    async def _apply_narrator_surface_contract(
        self,
        messages: list[ChatMessage],
        metadata: dict,
        acting_character_id: UUID | None,
    ) -> tuple[list[ChatMessage], dict]:
        audited = self._audit_prompt_policy(metadata)
        if acting_character_id is not None or not messages:
            return messages, audited
        first, *rest = messages
        layers = list(audited.get("included_layers") or [])
        if "layer_0a_narrator_surface" not in layers:
            layers.append("layer_0a_narrator_surface")
        audited["included_layers"] = layers
        return [
            ChatMessage(
                role=first.role,
                content=(
                    f"{first.content}\n\n"
                    f"{self._prompt_policy.narrator_surface_contract}"
                ),
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
        audited = self._audit_prompt_policy(metadata)
        if not acting_character_id or not messages:
            return messages, audited

        campaign = await self._campaign_repo.get_by_id(campaign_id)
        player_id = campaign.player_character_id if campaign else None
        if not player_id or player_id == acting_character_id:
            return messages, audited
        player = await self._entity_repo.get_character(player_id)
        actor = await self._entity_repo.get_character(acting_character_id)
        if not player or not actor:
            return messages, audited

        first, *rest = messages
        cleaned = self._remove_player_from_other_npcs(
            first.content,
            player.canonical_name,
        )
        cleaned = (
            f"{cleaned}\n\n"
            + self._prompt_policy.player_control_contract.format(
                player_name=player.canonical_name,
                actor_name=actor.canonical_name,
            )
        )
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
        messages, metadata = await self._context_pipeline.enrich(
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
        return messages, self._audit_prompt_policy(metadata)


__all__ = ["ContextCompiler", "count_tokens"]
