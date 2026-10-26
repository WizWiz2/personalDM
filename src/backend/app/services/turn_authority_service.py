from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.campaign_setup_repo import CampaignSetupRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.scene_repo import SceneRepository
from app.models.turn_authority import ExistingNpcArrival, TurnAuthority
from app.db.tables import Entity, Item, Location
from app.services.entity_identity import identity_key
from app.services.item_state_authority import load_item_states
from app.services.scene_state_service import SceneStateService
from app.services.outcome_fact_authority import established_state_lines, established_subjects
from app.services.turn_authority_planner import CoordinatedTurnPlan
from app.services.world_momentum_service import WorldMomentumService
from app.services.turn_authority_resolvers import (
    ActorResolver,
    AuthorityResolutionError,
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
        self._setups = CampaignSetupRepository(session)
        self._entities = EntityRepository(session)
        self._scene_state = SceneStateService(session)
        self._actors = ActorResolver(session)
        self._npc_introductions = NpcIntroductionResolver(session)

    async def _place_frame(self, campaign_id: UUID, location_id: str | None) -> dict:
        """Where the scene stands, from typed containment and exits only (replay15: Ладога from Молога)."""
        if not location_id or not isinstance(self._session, AsyncSession):
            return {}
        chain, cursor = [], location_id
        while cursor and len(chain) < 6:
            row = (await self._session.execute(
                select(Entity.canonical_name, Entity.description, Location.geography, Location.parent_location_id)
                .join(Location, Location.entity_id == Entity.id).where(Entity.id == cursor))).first()
            if row is None:
                break
            chain.insert(0, {"name": row[0], "profile": (row[2] or row[1] or "")[:300]})
            cursor = row[3]
        exits = await self._scene_state.list_exits(campaign_id, UUID(location_id))
        return {"here_inside_out": chain, "reachable_from_here": sorted({e.to_location_name for e in exits})}

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

        plot_source = getattr(target_state, "active_conflict", None)
        if (
            not plot_source and effective_scene_id and plan
            and callable(getattr(getattr(self, "_session", None), "execute", None))
        ):
            theses = await SceneRepository(self._session).list_theses_by_scene(
                effective_scene_id, active_only=True,
            )
            threads = [
                thesis for thesis in theses
                if thesis.thesis_type in {"unresolved_beat", "tension"}
                and thesis.priority >= 0
            ]
            if threads:
                plot_source = max(threads, key=lambda thesis: thesis.priority).text

        world_move = None
        if plan and plan.world_move and effective_scene_id:
            candidate = plan.world_move
            momentum = WorldMomentumService(self._session)
            authorized = await momentum.threads(campaign_id, effective_scene_id)
            matches = [
                source for source in authorized
                if source["source"].casefold().strip() == candidate.source.casefold().strip()
                and str(source["actor_id"] or "") == str(candidate.actor_id or "")
            ]
            # Only an NPC kind acts through the thread's actor; other kinds move nobody.
            actor_ok = (
                candidate.actor_id is not None
                if candidate.kind in {"npc_action", "npc_move"}
                else candidate.destination_location_id is None
            )
            route_ok = candidate.kind != "npc_move"
            if candidate.kind == "npc_move" and candidate.destination_location_id:
                route_ok = any(
                    str(candidate.destination_location_id) == edge["destination_location_id"]
                    for match in matches
                    for edge in match.get("available_destinations", [])
                ) and (
                    target_state is not None
                    and candidate.destination_location_id != target_state.location_id
                )
            clock_ok = (
                candidate.kind != "deadline" or (
                    candidate.due_phase is not None
                    and target_state is not None
                    and SceneStateService.normalize_day_part(
                        target_state.world_time_label
                    ) == candidate.due_phase
                )
            )
            previously_fired = (
                await momentum.deadline_already_triggered(
                    campaign_id, candidate.source,
                )
                if matches and candidate.kind == "deadline" and clock_ok
                else False
            )
            if (
                matches and actor_ok and route_ok and clock_ok and not previously_fired
                and candidate.outcome in plan.observable_consequences
                and plan.resolution not in {"failure", "uncertain"}
                and not plan.narration_policy.pending_player_choice
            ):
                world_move = candidate

        present_names = list(target_state.participant_names) if target_state else []
        present_keys = {identity_key(value) for value in present_names}

        actor = selected_actor
        effective_actor_id = selected_actor_id
        # The protagonist is controlled by the human. A routed /talk target or
        # stale actor provenance must never grant the protagonist an NPC beat:
        # beat validation expects third-person NPC prose, while the protagonist
        # is narrated in second person and cannot take autonomous actions.
        if actor and actor.id == campaign.player_character_id:
            actor = None
            effective_actor_id = None
        if actor and target_state and identity_key(actor.canonical_name) not in present_keys:
            actor = None
            effective_actor_id = None

        executed_steps = (
            (plan.scene_transition.execution_report or {}).get("steps", []) if plan else []
        )
        source_groups = {}
        for index, step in enumerate(executed_steps):
            source = step.get("source_action_index")
            source = step.get("step_index", index) if source is None else source
            source_groups.setdefault(source, []).append(step.get("status"))
        completed_indices = {source for source, statuses in source_groups.items()
                             if all(status == "completed" for status in statuses)}

        def prerequisite_completed(effect) -> bool:
            return effect.after_action_index is None or effect.after_action_index in completed_indices

        introductions = [
            npc for npc in (plan.npc_introductions if plan else [])
            if prerequisite_completed(npc)
        ]
        planned_response = plan.addressed_response if plan else None
        if planned_response and not prerequisite_completed(planned_response):
            planned_response = None
        # Always resolved: an unfilled resident slot authorizes its keeper even when the plan
        # introduces nobody (B9 T4: the post office's clerk spoke as an absent character).
        npc_resolver = getattr(self, "_npc_introductions", None) or NpcIntroductionResolver(self._session)
        try:
            npc_resolution = await npc_resolver.resolve(
                campaign_id=campaign_id,
                introductions=introductions,
                present_names=present_names,
                target_location_id=(target_state.location_id if target_state else None),
                addressee=(planned_response.speaker_name if planned_response else None),
            )
        except AuthorityResolutionError as exc:
            raise TurnAuthorityError(str(exc)) from exc

        present_names = list(npc_resolution.present_names)
        if world_move and world_move.kind == "arrival" and not world_move.actor_id and not any(
            npc.arrives for npc in npc_resolution.new_introductions
        ):
            world_move = None  # A stranger walks in only as a typed newcomer, never as prose alone.
        if world_move and world_move.kind == "arrival" and world_move.actor_id:
            # The thread's own person (e.g. the one sent for) is who the world brings here.
            arriving = await self._session.get(Entity, str(world_move.actor_id))
            if arriving and arriving.canonical_name not in present_names:
                npc_resolution.existing_arrivals.append(ExistingNpcArrival(
                    entity_id=world_move.actor_id, canonical_name=arriving.canonical_name,
                    reason=world_move.source[:500]))
                present_names.append(arriving.canonical_name)
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

        # Only read typed DB custody; never infer ownership from storyteller prose.
        # Thin authority contract tests inject repository doubles without a DB
        # session. Their typed cast behavior must remain independently testable.
        item_rows = []
        if callable(getattr(self._session, "execute", None)):
            item_rows = (await self._session.execute(
                select(Entity.canonical_name, Item.current_owner_id, Item.current_location_id)
                .join(Item, Item.entity_id == Entity.id)
                .where(Entity.campaign_id == str(campaign_id), Entity.entity_type == "item")
            )).all()
        current_location_id = str(target_state.location_id) if target_state and target_state.location_id else None
        unavailable_items = [
            name for name, owner_id, item_location_id in item_rows
            if owner_id != str(campaign.player_character_id or "")
            and (owner_id is not None or item_location_id != current_location_id)
        ]
        authority = TurnAuthority(
            place_frame=await self._place_frame(campaign_id, current_location_id),
            campaign_id=campaign_id,
            trigger_turn_id=trigger_turn_id,
            player_character_id=campaign.player_character_id,
            player_character_name=(player.canonical_name if player else None),
            acting_character_id=effective_actor_id,
            acting_character_name=(actor.canonical_name if actor else None),
            player_input=player_input,
            narrative_person=await self._setups.narrative_person(campaign_id),
            source_scene_id=source_scene_id,
            target_scene_id=effective_scene_id,
            scene_disposition=disposition,
            transition_type=transition_type,
            source_location_path=(list(source_state.location_path) if source_state else []),
            target_location_path=(list(target_state.location_path) if target_state else []),
            scene_time=getattr(target_state, "world_time_label", None),
            present_character_names=present_names,
            known_absent_character_names=absent_names,
            allowed_new_npcs=npc_resolution.new_introductions,
            allowed_new_items=(
                [
                    item for item in plan.item_introductions
                    if item.after_action_index in completed_indices
                    or (item.after_action_index is None and not (
                        plan.resolution in {"failure", "uncertain"}
                        or (executed_steps and not completed_indices)
                    ))
                ] if plan else []
            ),
            allowed_existing_npc_arrivals=npc_resolution.existing_arrivals,
            allowed_new_places=(list(plan.place_introductions)
                                if plan and plan.resolution not in {"failure", "uncertain"} else []),
            object_names=(list(target_state.object_names) if target_state else []),
            item_states=(await load_item_states(self._session, campaign_id)
                         if isinstance(self._session, AsyncSession) else []),
            item_states_loaded=isinstance(self._session, AsyncSession),
            unavailable_item_names=unavailable_items,
            world_move=world_move,
            resolution=(plan.resolution if plan else "conversation"),
            identity_reveal_requested=(
                bool(plan and (plan.personal_name_revealed or plan.identity_reveal_requested))
            ),
            dramatic_mode=(plan.narration_policy.dramatic_mode if plan else "calm"),
            observable_consequences=(
                [
                    value for value in plan.observable_consequences
                    if not (plan.world_move and world_move is None and value == plan.world_move.outcome)
                ] if plan else []
            ),
            character_beats=(list(plan.character_beats) if plan else []),
            addressed_response=(
                planned_response.model_copy(deep=True)
                if planned_response
                else None
            ),
            narration_guidance=(list(plan.narration_guidance) if plan else []),
            protected_player_decisions=(
                list(plan.narration_policy.protected_player_decisions) if plan else []
            ),
            pending_player_choice=(plan.narration_policy.pending_player_choice if plan else None),
            # Independent developments are permitted ONLY from an established
            # conflict. The narrator still cannot materialize a new NPC/item.
            allow_new_complication=bool(
                plan and (
                    world_move is not None
                    or plan.narration_policy.allow_new_complication
                    or (
                        plot_source
                        and plan.resolution not in {"failure", "uncertain"}
                        and not plan.narration_policy.pending_player_choice
                    )
                )
            ),
            complication_source=(
                world_move.source
                if world_move else plan.narration_policy.complication_source
                if plan and plan.narration_policy.allow_new_complication
                else plot_source
                if plan and plot_source and plan.resolution not in {"failure", "uncertain"}
                and not plan.narration_policy.pending_player_choice
                else None
            ),
            action_sequence=executed_sequence,
        )

        # The beat grant is the single source of NPC response: the /talk listener, else, when the
        # plan marks the player's line as addressed, the addressee the binding step resolved, else
        # the sole present NPC. A new introduction receives its ID when it is materialized.
        npcs = [
            name for name in present_names
            if identity_key(name) != identity_key(authority.player_character_name or "")
        ]
        sole = npcs[0] if len(npcs) == 1 else None
        addressed = bool(plan and plan.addressed_response_requested)
        owner_name = actor.canonical_name if actor else (
            (npc_resolution.addressee or sole) if addressed else None
        )
        owner = next(
            (entity for entity in all_characters
             if owner_name and identity_key(entity.canonical_name) == identity_key(owner_name)),
            None,
        )
        response = authority.addressed_response if owner_name else None
        update = {
            "beat_owner_id": owner.id if owner else None,
            "beat_owner_name": owner_name,
            "acting_character_id": owner.id if owner else None,
            "acting_character_name": owner_name,
            "addressed_response": (
                response.model_copy(update={
                    "speaker_name": owner_name, "speaker_id": owner.id if owner else None,
                }) if response else None
            ),
        }
        authority = authority.model_copy(update=update)

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
