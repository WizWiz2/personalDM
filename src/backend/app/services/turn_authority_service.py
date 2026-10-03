from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.models.turn_authority import ExistingNpcArrival, TurnAuthority
from app.services.entity_identity import exact_identity_matches, identity_key
from app.services.narrator_authority_contracts import (
    resolve_addressed_present_npc,
    should_assign_addressed_response_obligation,
)
from app.services.scene_state_service import SceneStateService
from app.services.outcome_fact_authority import established_state_lines, established_subjects
from app.services.turn_authority_planner import CoordinatedTurnPlan
from app.services.turn_authority_resolvers import (
    ActorResolver,
    AuthorityResolutionError,
    NpcIntroductionResolution,
    NpcIntroductionResolver,
)


class TurnAuthorityError(ValueError):
    """The planned turn cannot be represented as one coherent authority object."""


def _location_id_key(value: object) -> str | None:
    """Stable comparison key for location ids (UUID or str); None if unset/invalid."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return str(UUID(text))
    except (TypeError, ValueError):
        return None


def _address_repair_colocated(
    *,
    scene_location_id: object,
    character_location_id: object,
    player_location_id: object,
    same_scene_unplaced: bool = False,
) -> bool:
    """True when the named campaign entity shares the active scene's physical place.

    Live residual after #188: kitchen scenes may lack ``scene_location_links`` (null
    ``location_id``) while the PC already has ``current_location_id`` and the named
    addressee row is still unplaced. Requiring *all three* nulls then never promotes
    Лира, so obligation never stamps and sticky Housekeeper / Управляющая prose can
    publish. Anchor order:
    1. scene location when set;
    2. else PC location when set (unstructured scene, placed cast);
    3. unstructured scene (null scene location) + unplaced named campaign entity —
       same bag as live kitchen even when PC location is already set (not a cross-map
       teleport of a placed character; unplaced NPC is not invented into a *placed*
       scene).

    #190: when the scene *is* placed but the named addressee is still unplaced, allow
    co-locate only if ``same_scene_unplaced`` (first_seen_scene_id matches the effective
    scene). That repairs live kitchen Лира without teleports of placed cast elsewhere.
    """
    scene = _location_id_key(scene_location_id)
    character = _location_id_key(character_location_id)
    player = _location_id_key(player_location_id)
    anchor = scene or player
    if anchor is not None and character is not None:
        return anchor == character
    if character is not None:
        return False
    # Unplaced named campaign entity.
    if scene is None:
        return True
    return bool(same_scene_unplaced)


def _first_seen_scene_matches(entity: object, scene_id: object) -> bool:
    """True when entity.custom_fields.first_seen_scene_id equals the active scene."""
    if scene_id is None:
        return False
    fields = getattr(entity, "custom_fields", None)
    if not isinstance(fields, dict):
        return False
    raw = fields.get("first_seen_scene_id")
    if raw is None or not str(raw).strip():
        return False
    # UUID-normalize; scene ids share UUID shape with location keys.
    scene_key = _location_id_key(scene_id)
    seen_key = _location_id_key(raw)
    if scene_key and seen_key:
        return scene_key == seen_key
    return str(raw).strip() == str(scene_id).strip()


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
            await self._entities.get_character(selected_actor_id) if selected_actor_id else None
        )

        source_state = (
            await self._scene_state.get(campaign_id, source_scene_id) if source_scene_id else None
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

        executed_steps = (
            (plan.scene_transition.execution_report or {}).get("steps", []) if plan else []
        )
        completed_indices = {
            step.get("step_index", index)
            for index, step in enumerate(executed_steps)
            if step.get("status") == "completed"
        }

        def prerequisite_completed(effect) -> bool:
            return effect.after_action_index is None or effect.after_action_index in completed_indices

        introductions = [
            npc for npc in (plan.npc_introductions if plan else [])
            if prerequisite_completed(npc)
        ]
        planned_response = plan.addressed_response if plan else None
        if planned_response and not prerequisite_completed(planned_response):
            planned_response = None
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

        present_names = list(npc_resolution.present_names)
        present_keys = {identity_key(value) for value in present_names}
        all_characters = await self._entities.list_by_campaign(
            campaign_id,
            entity_type="character",
        )
        # Co-located known entities named in player_input but missing from scene participants
        # are repaired into present cast (no inventing people, no cross-location teleport).
        presence_arrivals: list[ExistingNpcArrival] = []
        campaign_cast = [
            entity.canonical_name
            for entity in all_characters
            if str(entity.canonical_name or "").strip()
        ]
        named = resolve_addressed_present_npc(
            player_input,
            campaign_cast,
            player_name=(player.canonical_name if player else None),
        )
        if named and identity_key(named) not in present_keys:
            match = next(
                (
                    entity
                    for entity in all_characters
                    if identity_key(entity.canonical_name) == identity_key(named)
                ),
                None,
            )
            if match is not None:
                character = await self._entities.get_character(match.id)
                target_loc = target_state.location_id if target_state else None
                char_loc = getattr(character, "current_location_id", None) if character else None
                player_loc = getattr(player, "current_location_id", None) if player else None
                same_scene_unplaced = _first_seen_scene_matches(
                    character if character is not None else match,
                    effective_scene_id,
                )
                if _address_repair_colocated(
                    scene_location_id=target_loc,
                    character_location_id=char_loc,
                    player_location_id=player_loc,
                    same_scene_unplaced=same_scene_unplaced,
                ):
                    present_names.append(match.canonical_name)
                    present_keys.add(identity_key(match.canonical_name))
                    presence_arrivals.append(
                        ExistingNpcArrival(
                            entity_id=match.id,
                            canonical_name=match.canonical_name,
                            reason=(
                                "Addressed known character is already at this location "
                                "but was missing from scene participants."
                            ),
                        )
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
            allowed_existing_npc_arrivals=[
                *npc_resolution.existing_arrivals,
                *presence_arrivals,
            ],
            object_names=(list(target_state.object_names) if target_state else []),
            resolution=(plan.resolution if plan else "conversation"),
            identity_reveal_requested=(
                bool(plan and (plan.personal_name_revealed or plan.identity_reveal_requested))
            ),
            dramatic_mode=(plan.narration_policy.dramatic_mode if plan else "calm"),
            observable_consequences=(list(plan.observable_consequences) if plan else []),
            character_beats=(list(plan.character_beats) if plan else []),
            addressed_response=(
                planned_response.model_copy(deep=True)
                if planned_response
                else None
            ),
            canon_constraints=(list(plan.canon_constraints) if plan else []),
            narration_guidance=(list(plan.narration_guidance) if plan else []),
            ending_hook=(plan.ending_hook if plan else ""),
            protected_player_decisions=(
                list(plan.narration_policy.protected_player_decisions) if plan else []
            ),
            pending_player_choice=(plan.narration_policy.pending_player_choice if plan else None),
            allow_new_complication=(
                plan.narration_policy.allow_new_complication if plan else False
            ),
            complication_source=(plan.narration_policy.complication_source if plan else None),
            action_sequence=executed_sequence,
        )

        addressee = should_assign_addressed_response_obligation(
            player_input,
            authority.present_character_names,
            player_name=authority.player_character_name,
            hinted_name=authority.acting_character_name,
            addressed_response_requested=bool(
                plan and getattr(plan, "addressed_response_requested", False)
                and (plan.addressed_response is None or planned_response is not None)
            ),
        )
        if addressee:
            # Typed addressee only: context for the narrator and acting-character binding,
            # never an obligation the narration is checked against.
            authority = authority.model_copy(update={"addressed_response_obligation": addressee})

        # Response ownership follows THIS turn's obligated addressee. Sticky `/talk` is only
        # input provenance: it must not keep a prior listener (and their dialogue history) when
        # the player names a different present cast member. Explicit actor-scoped callers
        # (acting_character_id argument) remain authoritative.
        if acting_character_id is None:
            from app.services.systemless_authority_guard import addressed_response_requested

            obligated = authority.addressed_response_obligation
            if obligated:
                obligated_key = identity_key(obligated)
                obligated_entity = next(
                    (
                        entity
                        for entity in all_characters
                        if identity_key(entity.canonical_name) == obligated_key
                        and identity_key(entity.canonical_name) in present_keys
                    ),
                    None,
                )
                if obligated_entity is not None:
                    if authority.acting_character_id != obligated_entity.id:
                        update = {
                            "acting_character_id": obligated_entity.id,
                            "acting_character_name": obligated_entity.canonical_name,
                        }
                        if planned_disposition == "stay":
                            update["scene_disposition"] = "actor_turn"
                            update["transition_type"] = "none"
                        authority = authority.model_copy(update=update)
                elif authority.acting_character_id is not None:
                    # Obligated designation is not a resolvable present entity — drop sticky
                    # so a prior listener's answered topic cannot bleed into this turn.
                    update = {
                        "acting_character_id": None,
                        "acting_character_name": None,
                    }
                    if authority.scene_disposition == "actor_turn":
                        update["scene_disposition"] = planned_disposition
                        if planned_disposition == "stay":
                            update["transition_type"] = "none"
                    authority = authority.model_copy(update=update)
            elif authority.acting_character_id is not None:
                # No obligation: sticky listener stays only when the planner marks addressed
                # response ownership for this turn; otherwise clear.
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

            # Defense in depth after #188: THIS turn uniquely names a different campaign
            # entity than the sticky /talk listener (e.g. Лира vs Housekeeper). Never leave
            # the prior EN twin / steward as acting character for that address — even when
            # promotion/obligation failed and addressed_response_requested would otherwise
            # retain sticky ownership.
            if (
                named
                and authority.acting_character_id is not None
                and identity_key(named) != identity_key(authority.acting_character_name or "")
            ):
                update = {
                    "acting_character_id": None,
                    "acting_character_name": None,
                }
                if authority.scene_disposition == "actor_turn":
                    update["scene_disposition"] = planned_disposition
                    if planned_disposition == "stay":
                        update["transition_type"] = "none"
                authority = authority.model_copy(update=update)

        if plan and plan.addressed_response and planned_response is None:
            authority.acting_character_id = None
            authority.acting_character_name = None
            authority.addressed_response_obligation = None

        if authority.addressed_response:
            response = authority.addressed_response
            # A typed introduction is part of the post-turn cast even before it has a database ID.
            # Bind aliases to an existing ID; a response must not invent or rename its owner.
            response_cast = list(dict.fromkeys([
                *authority.present_character_names, *authority.allowed_new_npc_names,
                *authority.allowed_existing_npc_arrival_names,
            ]))
            present_entities = [
                entity for entity in all_characters
                if identity_key(entity.canonical_name) in {identity_key(n) for n in response_cast}
                and entity.id != authority.player_character_id
            ]
            designation = (
                response.speaker_name or authority.addressed_response_obligation
                or authority.acting_character_name or ""
            )
            matches = exact_identity_matches(present_entities, designation)
            if response.speaker_id is not None:
                id_matches = [e for e in present_entities if e.id == response.speaker_id]
                if len(id_matches) != 1 or (matches and matches[0].id != response.speaker_id):
                    raise TurnAuthorityError("Planned response speaker ID is absent or inconsistent")
                matches = id_matches
            if len(matches) > 1:
                raise TurnAuthorityError("Planned response speaker identity is ambiguous")
            speaker = resolve_addressed_present_npc(
                matches[0].canonical_name if matches else designation,
                response_cast,
                player_name=authority.player_character_name,
            )
            speaker_entity = next(
                (
                    entity
                    for entity in all_characters
                    if speaker and identity_key(entity.canonical_name) == identity_key(speaker)
                ),
                None,
            )
            if response.speaker_name and not speaker:
                # The planner named someone who is not in this scene. Drop the reply
                # instead of inventing them or aborting the turn.
                speaker = None
                unbound = True
            else:
                unbound = False
            if not speaker and not unbound and plan and plan.addressed_response_requested:
                non_player = [
                    name for name in response_cast
                    if identity_key(name) != identity_key(authority.player_character_name or "")
                ]
                if len(non_player) == 1:
                    speaker = non_player[0]
                    speaker_entity = next(
                        (entity for entity in present_entities
                         if identity_key(entity.canonical_name) == identity_key(speaker)), None,
                    )
                else:
                    # Nobody present, or more than one: the line is not a bound reply.
                    unbound = True
            if unbound:
                authority.addressed_response = None
                authority.addressed_response_obligation = None
                authority.acting_character_id = None
                authority.acting_character_name = None
            else:
                authority.addressed_response = response.model_copy(
                    update={
                        "speaker_name": speaker,
                        "speaker_id": speaker_entity.id if speaker_entity else None,
                    }
                )
                if speaker_entity:
                    # The frozen semantic addressee outranks a stale /talk selection. Persist
                    # ownership by ID so a later public name reveal does not change the actor.
                    authority.acting_character_id = speaker_entity.id
                    authority.acting_character_name = speaker_entity.canonical_name
                if speaker:
                    authority.addressed_response_obligation = speaker

        if plan and plan.addressed_response_requested:
            # The addressed beat belongs to the bound listener (/talk or obligation), else to the
            # sole present NPC. Typed cast identity only; the narration check verifies it acted.
            present_npcs = [
                entity for entity in all_characters
                if identity_key(entity.canonical_name) in present_keys
                and entity.id != authority.player_character_id
            ]
            owner = next(
                (entity for entity in present_npcs if entity.id == authority.acting_character_id),
                present_npcs[0] if len(present_npcs) == 1 else None,
            )
            if owner is not None:
                authority.beat_owner_id = owner.id
                authority.beat_owner_name = owner.canonical_name

        lines = await established_state_lines(
            self._session,
            campaign_id,
            effective_scene_id,
        )
        subjects = await established_subjects(
            self._session,
            campaign_id,
            effective_scene_id,
        )
        slot_update = {}
        if lines:
            slot_update["established_state"] = lines
        if subjects:
            slot_update["established_subjects"] = subjects
        if slot_update:
            authority = authority.model_copy(update=slot_update)

        return authority
