from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.models.turn_authority import ExistingNpcArrival, TurnAuthority
from app.services.entity_identity import identity_key, resolve_character_candidates
from app.services.scene_state_service import SceneStateService
from app.services.turn_authority_planner import CoordinatedTurnPlan


class TurnAuthorityError(ValueError):
    """The planned turn cannot be represented as one coherent authority object."""


class TurnAuthorityService:
    """Build the sole narrator/validator authority from structured state plus the plan.

    ``acting_character_id`` on player input identifies the NPC currently addressed by the UI. It
    does not itself prove that the NPC may own the resulting assistant turn. Planning always happens
    first; the selected NPC becomes the effective actor only when they are still a participant of
    the target scene and the structured plan stays in that scene. This lets one input contain both
    player action and dialogue without turning `/talk` into a Planner bypass.

    Entity identity is canonicalized here rather than delegated back to the control model. Exact
    names and aliases are compared through a mixed-script-safe key. A temporary generic role may
    also resolve to one known character already at the target location. Cross-location identity
    repair remains forbidden.
    """

    def __init__(self, session: AsyncSession):
        self._session = session
        self._campaigns = CampaignRepository(session)
        self._entities = EntityRepository(session)
        self._scene_state = SceneStateService(session)

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
        selected_actor = (
            await self._entities.get_character(acting_character_id)
            if acting_character_id
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
        all_characters = await self._entities.list_by_campaign(
            campaign_id,
            entity_type="character",
        )

        # UI selection is an address target, not unconditional actor authority. If structured
        # execution moved/focused the turn to a scene where that character is not present, they
        # cannot answer remotely and their identity must not become memory provenance.
        actor = selected_actor
        effective_actor_id = acting_character_id
        if actor and target_state and identity_key(actor.canonical_name) not in present_keys:
            actor = None
            effective_actor_id = None

        character_states = {}
        character_locations: dict[UUID, UUID | None] = {}
        for entity in all_characters:
            character = await self._entities.get_character(entity.id)
            if not character:
                continue
            entity_id = UUID(str(entity.id))
            character_states[entity_id] = character
            character_locations[entity_id] = character.current_location_id

        target_location_id = target_state.location_id if target_state else None
        introductions = []
        existing_arrivals: list[ExistingNpcArrival] = []
        for introduction in list(plan.npc_introductions) if plan else []:
            matches = resolve_character_candidates(
                all_characters,
                proposed_name=introduction.canonical_name,
                proposed_role=introduction.role,
                temporary_name=introduction.temporary_name,
                target_location_id=target_location_id,
                character_locations=character_locations,
            )
            unique_matches = {UUID(str(entity.id)): entity for entity in matches}
            if len(unique_matches) > 1:
                names = ", ".join(
                    sorted(entity.canonical_name for entity in unique_matches.values())
                )
                raise TurnAuthorityError(
                    "Planner NPC identity is ambiguous for "
                    f"{introduction.canonical_name}: {names}"
                )
            if not unique_matches:
                introductions.append(introduction)
                continue

            existing_id, existing = next(iter(unique_matches.items()))
            existing_key = identity_key(existing.canonical_name)
            if existing_key in present_keys:
                continue

            character = character_states.get(existing_id)
            if (
                character
                and target_location_id
                and character.current_location_id == target_location_id
            ):
                existing_arrivals.append(
                    ExistingNpcArrival(
                        entity_id=existing_id,
                        canonical_name=existing.canonical_name,
                        reason=introduction.reason,
                    )
                )
                present_names.append(existing.canonical_name)
                present_keys.add(existing_key)
                continue

            location = (
                str(character.current_location_id)
                if character and character.current_location_id
                else "неизвестна"
            )
            target = str(target_location_id) if target_location_id else "неизвестна"
            raise TurnAuthorityError(
                "Известный персонаж не может появиться без структурного перемещения: "
                f"{existing.canonical_name} находится в {location}, target location = {target}"
            )

        absent_names = [
            entity.canonical_name
            for entity in all_characters
            if identity_key(entity.canonical_name) not in present_keys
        ]

        planned_disposition = plan.scene_disposition if plan else "stay"
        # A selected, still-present NPC owns a conversational assistant turn only when structured
        # planning kept the scene boundary stable. Movement/sequence/focus outcomes remain normal
        # narrated turns even if the input also addressed that NPC.
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

        return TurnAuthority(
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
            allowed_new_npcs=introductions,
            allowed_existing_npc_arrivals=existing_arrivals,
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


__all__ = ["TurnAuthorityError", "TurnAuthorityService"]
