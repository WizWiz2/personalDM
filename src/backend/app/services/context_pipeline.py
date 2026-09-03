from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.repositories.narrative_detail_repo import NarrativeDetailRepository
from app.db.tables import Campaign, Entity, Item
from app.models.turn import ChatMessage
from app.services.scene_bridge_service import SceneBridgeService
from app.services.scene_state_service import SceneStateService

TokenCounter = Callable[[str], int]


@dataclass(frozen=True)
class ContextRequest:
    campaign_id: UUID
    acting_character_id: UUID | None = None
    scene_id: UUID | None = None
    current_user_content: str | None = None
    max_budget_override: int | None = None


@dataclass(frozen=True)
class CompiledContext:
    messages: list[ChatMessage]
    metadata: dict


class ContextProvider(Protocol):
    """An explicit, ordered context enrichment stage."""

    name: str

    async def enrich(
        self,
        request: ContextRequest,
        context: CompiledContext,
    ) -> CompiledContext: ...


class ContextPipeline:
    """Apply context providers in a visible and testable order."""

    def __init__(self, providers: Sequence[ContextProvider]):
        self.providers = tuple(providers)

    @property
    def provider_names(self) -> tuple[str, ...]:
        return tuple(provider.name for provider in self.providers)

    async def enrich(
        self,
        request: ContextRequest,
        messages: list[ChatMessage],
        metadata: dict,
    ) -> tuple[list[ChatMessage], dict]:
        context = CompiledContext(messages=list(messages), metadata=dict(metadata))
        for provider in self.providers:
            context = await provider.enrich(request, context)

        audited = dict(context.metadata)
        audited["context_pipeline"] = list(self.provider_names)
        return context.messages, audited


class SceneStateContextProvider:
    """Inject authoritative scene state, action references and the incoming scene bridge."""

    name = "authoritative_scene_state"

    def __init__(self, session: AsyncSession):
        self._session = session

    async def _action_reference_contract(
        self,
        request: ContextRequest,
        state,
    ) -> tuple[str, list[str]]:
        """Expose exact durable ids needed by Planner-owned inventory semantics.

        The executor never guesses entities from prose. Planner therefore needs exact item and
        character ids in the same authoritative context it uses to decide the semantic action.
        """
        campaign = await self._session.get(Campaign, str(request.campaign_id))
        owned_rows: list[tuple[str, str]] = []
        if campaign and campaign.player_character_id:
            result = await self._session.execute(
                select(Entity.id, Entity.canonical_name)
                .join(Item, Item.entity_id == Entity.id)
                .where(
                    Entity.campaign_id == str(request.campaign_id),
                    Item.current_owner_id == campaign.player_character_id,
                )
                .order_by(Entity.canonical_name)
            )
            owned_rows = [(str(entity_id), name) for entity_id, name in result.all()]

        owned = ", ".join(
            f"{name} [id={entity_id}]" for entity_id, name in owned_rows
        ) or "none recorded"
        present = ", ".join(
            f"{name} [id={entity_id}]"
            for entity_id, name in zip(state.participant_ids, state.participant_names)
        ) or "none recorded"
        section = (
            "[STRUCTURED ACTION REFERENCES]\n"
            f"Player-owned items: {owned}\n"
            f"Physically present characters: {present}\n"
            "Planner inventory contract:\n"
            "- Any explicit take/drop/place/give that changes durable item ownership or location "
            "MUST be emitted as an inventory action_sequence step, even when it is the only "
            "committed world action in the turn.\n"
            "- Reuse item_id and inventory_target_id exactly from the authoritative ids above or "
            "from Objects physically here; never invent, infer, rename or fabricate an id.\n"
            "- take uses an object physically here; drop/place/give uses a player-owned item.\n"
            "- give requires inventory_target_id for a physically present character.\n"
        )
        return section, [entity_id for entity_id, _ in owned_rows]

    async def enrich(
        self,
        request: ContextRequest,
        context: CompiledContext,
    ) -> CompiledContext:
        if not request.scene_id or not context.messages:
            return context

        try:
            state = await SceneStateService(self._session).get(
                request.campaign_id,
                request.scene_id,
            )
        except ValueError:
            return context

        bridge = await SceneBridgeService(self._session).get_for_target_scene(
            request.campaign_id,
            request.scene_id,
        )
        action_references, owned_item_ids = await self._action_reference_contract(request, state)
        first, *rest = context.messages
        contracts = [SceneStateService.prompt_contract(state), action_references]
        if bridge:
            contracts.append(SceneBridgeService.prompt_contract(bridge))
        messages = [
            ChatMessage(
                role="system",
                content=f"{first.content}\n\n" + "\n".join(contracts),
            ),
            *rest,
        ]

        metadata = dict(context.metadata)
        metadata["scene_state"] = {
            "scene_id": str(state.scene_id),
            "location_id": str(state.location_id) if state.location_id else None,
            "location_path": state.location_path,
            "world_time_label": state.world_time_label,
            "world_time_order": state.world_time_order,
            "participant_ids": [str(value) for value in state.participant_ids],
            "object_ids": [str(value) for value in state.object_ids],
            "available_exit_ids": [str(item.id) for item in state.available_exits],
            "available_destination_ids": [
                str(item.to_location_id) for item in state.available_exits
            ],
            "invariant_errors": state.invariant_errors,
        }
        included_item_ids = list(metadata.get("included_item_ids") or [])
        for item_id in owned_item_ids:
            if item_id not in included_item_ids:
                included_item_ids.append(item_id)
        metadata["included_item_ids"] = included_item_ids
        if bridge:
            metadata["scene_bridge"] = bridge.model_dump(mode="json")
        layers = list(metadata.get("included_layers") or [])
        if "layer_1_authoritative_scene_state" not in layers:
            layers.append("layer_1_authoritative_scene_state")
        if "layer_1_structured_action_references" not in layers:
            layers.append("layer_1_structured_action_references")
        if bridge and "layer_1_scene_bridge" not in layers:
            layers.append("layer_1_scene_bridge")
        metadata["included_layers"] = layers
        return CompiledContext(messages=messages, metadata=metadata)


class NarrativeDetailsContextProvider:
    """Inject recent transient scene texture without promoting it to canon."""

    name = "recent_narrative_details"

    def __init__(self, session: AsyncSession, token_counter: TokenCounter):
        self._session = session
        self._token_counter = token_counter

    async def enrich(
        self,
        request: ContextRequest,
        context: CompiledContext,
    ) -> CompiledContext:
        metadata = dict(context.metadata)
        metadata.setdefault("included_narrative_detail_ids", [])
        if not request.scene_id or not context.messages:
            return CompiledContext(messages=context.messages, metadata=metadata)

        details = await NarrativeDetailRepository(self._session).list_recent(
            request.campaign_id,
            request.scene_id,
            visibility="public" if request.acting_character_id else None,
            turn_window=settings.NARRATIVE_DETAIL_TURN_WINDOW,
            max_items=settings.NARRATIVE_DETAIL_MAX_ITEMS,
        )
        if not details:
            return CompiledContext(messages=context.messages, metadata=metadata)

        section = (
            "[Recent Scene Texture — transient, non-canon]\n"
            + "".join(f"- {detail.text}\n" for detail in details)
            + "Use these details only for immediate visual and sensory continuity. "
            "Do not turn them into stable personality traits, beliefs, promises, "
            "world laws or campaign facts unless a later authoritative outcome "
            "explicitly establishes such a change.\n"
        )
        section_tokens = self._token_counter(section)
        budget_max = int(metadata.get("token_budget_max") or 0)
        budget_used = int(metadata.get("token_budget_used") or 0)
        if budget_max and budget_used + section_tokens >= budget_max:
            metadata["narrative_details_omitted_for_budget"] = len(details)
            return CompiledContext(messages=context.messages, metadata=metadata)

        first = context.messages[0]
        messages = [
            ChatMessage(role=first.role, content=f"{first.content}\n\n{section}"),
            *context.messages[1:],
        ]
        metadata["token_budget_used"] = budget_used + section_tokens
        metadata["included_narrative_detail_ids"] = [
            str(detail.id) for detail in details
        ]
        layers = list(metadata.get("included_layers") or [])
        layers.append("layer_1b_recent_narrative_details")
        metadata["included_layers"] = layers
        return CompiledContext(messages=messages, metadata=metadata)