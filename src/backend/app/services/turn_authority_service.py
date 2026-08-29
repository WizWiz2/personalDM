from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.models.turn_authority import TurnAuthority
from app.services.entity_identity import identity_key
from app.services.scene_state_service import SceneStateService
from app.services.turn_authority_planner import CoordinatedTurnPlan
from app.services.turn_authority_resolvers import (
    ActorResolver,
    AuthorityResolutionError,
    NpcIntroductionResolution,
    NpcIntroductionResolver,
)


class TurnAuthorityError(ValueError):
    """The planned turn cannot be represented as one coherent authority object."""


class TurnAuthorityService:
    """Assemble the sole narrator/validator authority from structured state plus the plan.

    Resolution policies for selected actors and NPC introductions live in dedicated collaborators;
    this service owns composition of the final TurnAuthority and its cross-cutting disposition rules.
    """

    def __init__(self, session: AsyncSession):
        self._session = session
        self._campaigns = CampaignRepository(session)
        self._entities = EntityRepository(session)
        self._scene_state = SceneStateService(session)
        self._actors = ActorResolver(session)
        self._npc_introductions = NpcIntroductionResolver(session)

    async def build(
        self,
        *,
        campaign_id: UUID,
        trigger_turn_id: UUID,
        player_input: str,
        source_scene_id: UUID | None,
        target_scene_id: UUID | None,
        plan: CoordinatedTurnPlan | None,
        acting_character_id: UUID | None,
    ) -> TurnAuthority:
        campaign = await self._campaigns.get_by_id(campaign_id)
        if not campaign:
            raise TurnAuthorityError("Campaign not found while building turn authority")

        player = (
            await self._entities.get_character(campaign.player_character_id)
            if campaign.player_character_id
            else None
        )
        # Focused contract tests construct the assembler with __new__ and inject repository doubles.
        # Keep collaborators lazily composable without putting their policy back into this service.
        actor_resolver = getattr(self, "_actors", None) or ActorResolver(self._session)
        selected_actor_id = await actor_resolver.resolve_id(
            trigger_turn_id,
            acting_character_id,
        )
        selected_actor = (
            await self._entities.get_character(selected_actor_id)
            if selected_actor_id
            else None
        )

        source_state = (
            await self._scene_state.get(campaign_id, source_scene_id)
            if source_scene_id
            else None
        )
        effective_scene_id = target_scene_id or source_scene_id
        target_state = (
            await self._scene_state.get(campaign_id, effective_scene_id)
            if effective_scene_id
            else None
        )

        present_names = list(target_state.participant_names) if target_state else []
        present_keys = {identity_key(value) for value in present_names}

        actor = selected_actor
        effective_actor_id = selected_actor_id
        if actor and target_state and identity_key(actor.canonical_name) not in present_keys:
            actor = None
            effective_actor_id = None

        introductions = list(plan.npc_introductions) if plan else []
        if introductions:
            npc_resolver = getattr(self, "_npc_introductions", None) or NpcIntroductionResolver(
                self._session
            )
            try:
                npc_resolution = await npc_resolver.resolve(
                    campaign_id=campaign_id,
                    introductions=introductions,
                    present_names=present_names,
                    target_location_id=(target_state.location_id if target_state else None),
                )
            except AuthorityResolutionError as exc:
                raise TurnAuthorityError(str(exc)) from exc
        else:
            npc_resolution = NpcIntroductionResolution(
                new_introductions=[],
                existing_arrivals=[],
                present_names=present_names,
            )

        present_names = npc_resolution.present_names
        present_keys = {identity_key(value) for value in present_names}
        all_characters = await self._entities.list_by_campaign(
            campaign_id,
            entity_type="character",
        )
        absent_names = [
            entity.canonical_name
            for entity in all_characters
            if identity_key(entity.canonical_name) not in present_keys
        ]

        planned_disposition = plan.scene_disposition if plan else "stay"
        disposition = (
            "actor_turn"
            if actor is not None and planned_disposition == "stay"
            else planned_disposition
        )
        transition_type = "none"
        if plan and plan.scene_transition.required:
            transition_type = plan.scene_transition.transition_type
        if plan and disposition == "sequence":
            transition_type = "action_sequence"

        executed_sequence = None
        if plan and plan.scene_transition.execution_report:
            executed_sequence = dict(plan.scene_transition.execution_report)
        elif plan and plan.action_sequence.steps:
            executed_sequence = {
                "status": "planned_not_executed",
                "planned": plan.action_sequence.model_dump(mode="json"),
            }

        authority = TurnAuthority(
            campaign_id=campaign_id,
            trigger_turn_id=trigger_turn_id,
            player_character_id=campaign.player_character_id,
            player_character_name=(player.canonical_name if player else None),
            acting_character_id=effective_actor_id,
            acting_character_name=(actor.canonical_name if actor else None),
            player_input=player_input,
            source_scene_id=source_scene_id,
            target_scene_id=effective_scene_id,
            scene_disposition=disposition,
            transition_type=transition_type,
            source_location_path=(list(source_state.location_path) if source_state else []),
            target_location_path=(list(target_state.location_path) if target_state else []),
            present_character_names=present_names,
            known_absent_character_names=absent_names,
            allowed_new_npcs=npc_resolution.new_introductions,
            allowed_existing_npc_arrivals=npc_resolution.existing_arrivals,
            object_names=(list(target_state.object_names) if target_state else []),
            resolution=(plan.resolution if plan else "conversation"),
            dramatic_mode=(plan.narration_policy.dramatic_mode if plan else "calm"),
            observable_consequences=(list(plan.observable_consequences) if plan else []),
            character_beats=(list(plan.character_beats) if plan else []),
            canon_constraints=(list(plan.canon_constraints) if plan else []),
            narration_guidance=(list(plan.narration_guidance) if plan else []),
            ending_hook=(plan.ending_hook if plan else ""),
            protected_player_decisions=(
                list(plan.narration_policy.protected_player_decisions) if plan else []
            ),
            pending_player_choice=(
                plan.narration_policy.pending_player_choice if plan else None
            ),
            allow_new_complication=(
                plan.narration_policy.allow_new_complication if plan else False
            ),
            complication_source=(
                plan.narration_policy.complication_source if plan else None
            ),
            action_sequence=executed_sequence,
        )

        # Sticky `/talk` identifies a possible listener, not unconditional ownership of every later
        # player action. Explicit actor-scoped internal callers remain authoritative; public routing
        # may assign a response actor only when the current input actually addresses that character.
        if acting_character_id is None and authority.acting_character_id is not None:
            from app.services.systemless_authority_guard import addressed_response_requested

            if not addressed_response_requested(player_input, plan):
                update = {
                    "acting_character_id": None,
                    "acting_character_name": None,
                }
                if authority.scene_disposition == "actor_turn":
                    update["scene_disposition"] = planned_disposition
                    if planned_disposition == "stay":
                        update["transition_type"] = "none"
                authority = authority.model_copy(update=update)

        return authority


__all__ = ["TurnAuthorityError", "TurnAuthorityService"]
