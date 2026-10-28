from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.location_repo import LocationRepository
from app.db.tables import Campaign, Entity
from app.models.item_state import ItemStateSnapshot
from app.models.player_intent import (
    ActionOutcomeDecision,
    PlayerActionIntent,
    PlayerIntentContract,
    TurnOutcomeDecision,
)
from app.models.turn_authority import PlannedNpcIntroduction
from app.services.entity_identity import identity_key
from app.services.item_state_authority import load_item_states
from app.services.scene_state_service import SceneStateService
from app.services.turn_authority_planner import CoordinatedTurnPlan
from app.services.turn_planner import (
    ActionSequencePlan,
    ActionStepPlan,
    NarrationPolicy,
    PlanContractError,
    SceneTransitionPlan,
    TurnPlanningError,
)


@dataclass(frozen=True)
class MissingDestinationProfile:
    action_index: int
    destination: str
    origin: str | None = None


class ActionPlanCompiler:
    """Compile frozen human intent + external outcomes into executable engine structures.

    This class owns route topology. It never parses the human sentence. Every movement hop is
    checked against the virtual location reached by the previous *successful* compiled hop, which
    makes compound movement a graph compilation problem instead of repeated NLP.
    """

    def __init__(self, session: AsyncSession):
        self._session = session
        self._locations = LocationRepository(session)
        self._state = SceneStateService(session)

    @staticmethod
    def _blocked(action: PlayerActionIntent, reason: str, public: str) -> ActionStepPlan:
        return ActionStepPlan(
            action_type="movement",
            intent=action.intent,
            resolution="blocked",
            safe_mundane=False,
            blocking_reason=reason,
            public_blocking_reason=public,
        )

    async def _route_blocker(
        self, campaign_id: UUID, source_id: UUID, target_id: UUID
    ) -> tuple[str, str] | None:
        """None when a discovered open path joins the places; otherwise (internal, public) reason.

        A known place reachable over known open routes is one ordinary trip, however many edges.
        """
        if source_id == target_id:
            return None
        exits = await self._state.list_exits(campaign_id, source_id, include_hidden=True)
        direct = next((item for item in exits if item.to_location_id == target_id), None)
        if direct is not None and not direct.active:
            return (
                "Destination route is currently inactive",
                direct.access_rule or "Путь туда сейчас недоступен.",
            )
        if direct is not None and not direct.discovered:
            return "Destination exit has not been discovered.", "Путь туда пока не обнаружен."
        if direct is not None:
            return None
        seen, frontier = {source_id}, [source_id]
        while frontier:
            following = []
            for location_id in frontier:
                for item in await self._state.list_exits(campaign_id, location_id):
                    if item.access_rule or item.to_location_id in seen:
                        continue
                    if item.to_location_id == target_id:
                        return None
                    seen.add(item.to_location_id)
                    following.append(item.to_location_id)
            frontier = following
        return (
            "Destination is not reachable over known open routes.",
            "Из текущего места туда нет доступного прохода.",
        )

    async def _world(self, campaign_id: UUID):
        campaign = await self._session.get(Campaign, str(campaign_id))
        if campaign is None or not campaign.current_scene_id:
            raise TurnPlanningError("intent compiler requires an active campaign scene")
        scene_id = UUID(str(campaign.current_scene_id))
        state = await self._state.get(campaign_id, scene_id)
        locations = await self._locations.list_by_campaign(campaign_id)
        return scene_id, state, locations

    @staticmethod
    def _outcome_map(contract: PlayerIntentContract, decision: TurnOutcomeDecision):
        expected = set(range(len(contract.actions)))
        by_index = {}
        for item in decision.action_outcomes:
            if item.action_index in by_index:
                raise PlanContractError(
                    f"outcome resolver returned duplicate action_index={item.action_index}"
                )
            by_index[item.action_index] = item
        if set(by_index) != expected:
            raise PlanContractError(
                "outcome resolver must return exactly one outcome per frozen action; "
                f"expected={sorted(expected)} got={sorted(by_index)}"
            )
        return by_index

    async def resolve_known_travel(
        self, campaign_id: UUID, contract: PlayerIntentContract
    ) -> TurnOutcomeDecision | None:
        """Resolve ordinary graph travel without asking a model to invent feasibility.

        Explicit contact, conditional access, active conflicts and unknown destinations still need
        external resolution. For an ordinary trip on known topology, the graph is the authority;
        movement alone does not authorize introducing a new person or moving a bystander.
        """
        if (
            not contract.actions
            or contract.addressed_response_requested
            or contract.pending_player_choice
            or any(action.action_type != "movement" for action in contract.actions)
            or any(action.destination_location_id is None for action in contract.actions)
            or any(action.movement_method != "ordinary" for action in contract.actions)
            or any(action.requested_companions for action in contract.actions)
        ):
            return None
        _, state, locations = await self._world(campaign_id)
        if not state.location_id or getattr(state, "active_conflict", None):
            return None
        by_id = {item.id: item for item in locations}
        cursor = (state.location_id, None)
        outcomes = []
        for index, action in enumerate(contract.actions):
            if outcomes and outcomes[-1].resolution != "auto_success":
                outcomes.append(ActionOutcomeDecision(
                    action_index=index,
                    resolution="blocked",
                    blocking_reason="Предыдущий переход не выполнен.",
                ))
                continue
            if action.destination_location_id == cursor[0] and cursor[1] is None:
                return None  # the same place: the resolver describes the local step
            exits = await self._state.list_exits(campaign_id, cursor[0], include_hidden=True)
            if any(
                item.access_rule and item.to_location_id == action.destination_location_id
                for item in exits
            ):
                return None
            candidate = ActionOutcomeDecision(
                action_index=index,
                resolution="auto_success",
                safe_mundane=True,
                observable_outcome=f"Переход в место «{action.destination_location}» завершён.",
            )
            step, cursor, _ = await self._compile_movement(
                campaign_id=campaign_id, action=action, outcome=candidate,
                cursor=cursor, by_id=by_id,
            )
            outcomes.append(ActionOutcomeDecision(
                action_index=index,
                resolution=step.resolution,
                safe_mundane=step.safe_mundane,
                observable_outcome=step.observable_outcome,
                blocking_reason=step.blocking_reason,
            ))
        return TurnOutcomeDecision(action_outcomes=outcomes, resolution="sequence")

    async def resident_slots(self, campaign_id: UUID) -> list[tuple[str, str, bool]]:
        """Typed resident slots here and in the places containing here: (location ID, role, filled)."""
        _scene_id, state, locations = await self._world(campaign_id)
        by_id = {item.id: item for item in locations}
        holders = {
            json.loads(row or "{}").get("slot_id")
            for row in (await self._session.execute(
                select(Entity.custom_fields).where(Entity.entity_type == "character")
            )).scalars()
        }
        slots, place = [], by_id.get(state.location_id)
        while place is not None:
            role = (place.custom_fields or {}).get("resident_role")
            if role:
                slots.append((str(place.id), role, str(place.id) in holders))
            place = by_id.get(place.parent_location_id)
        return slots

    async def missing_destination_profiles(
        self,
        campaign_id: UUID,
        contract: PlayerIntentContract,
        decision: TurnOutcomeDecision,
    ) -> list[MissingDestinationProfile]:
        """Every successful trip to a place not yet catalogued gets a generated name and profile."""
        _scene_id, state, locations = await self._world(campaign_id)
        names = {item.id: item.canonical_name for item in locations}
        outcome_by_index = self._outcome_map(contract, decision)
        origin = names.get(state.location_id)
        missing: list[MissingDestinationProfile] = []
        for index, action in enumerate(contract.actions):
            if action.action_type != "movement":
                continue
            outcome = outcome_by_index[index]
            if (
                outcome.resolution == "auto_success"
                and action.destination_location_id is None
                and not outcome.destination
            ):
                missing.append(MissingDestinationProfile(index, action.destination_location, origin))
            origin = names.get(action.destination_location_id, action.destination_location)
        return missing

    async def _compile_movement(
        self,
        *,
        campaign_id: UUID,
        action: PlayerActionIntent,
        outcome,
        cursor: tuple[UUID | None, tuple[str, str | None] | None],
        by_id: dict[UUID, object],
    ) -> tuple[ActionStepPlan, tuple, bool]:
        """Compile one hop from the virtual cursor (known place, pending new place this turn)."""
        if outcome.resolution != "auto_success":
            return (
                ActionStepPlan(
                    action_type="movement",
                    intent=action.intent,
                    resolution=outcome.resolution,
                    safe_mundane=False,
                    observable_outcome=outcome.observable_outcome,
                    blocking_reason=outcome.blocking_reason,
                    public_blocking_reason=outcome.blocking_reason,
                ),
                cursor,
                False,
            )
        location_id, pending = cursor
        current = by_id.get(location_id)
        if current is None:
            return (
                self._blocked(
                    action,
                    "Current physical location is unavailable for route compilation.",
                    "Не удалось определить исходное место; нужно уточнить, откуда идти.",
                ),
                cursor,
                False,
            )
        target = by_id.get(action.destination_location_id)
        if target is not None and target.id == location_id and pending is None:
            # The same place: a step inside it, not a trip.
            step = self._compile_nonmovement(action.model_copy(update={
                "action_type": "interaction", "destination_location": None,
                "destination_location_id": None, "requested_companions": [],
            }), outcome)
            return step, cursor, False
        if target is not None:
            # A place created earlier this turn is joined only to its origin, the cursor.
            blocker = await self._route_blocker(campaign_id, location_id, target.id)
            if blocker is not None:
                return self._blocked(action, *blocker), cursor, False
            transition = SceneTransitionPlan(
                required=True,
                transition_type="location_transition",
                destination_location=target.canonical_name,
                destination_location_id=target.id,
                reason=action.intent,
                bridge_summary=outcome.observable_outcome or action.intent,
            )
            return (
                ActionStepPlan(
                    action_type="movement",
                    intent=action.intent,
                    resolution="auto_success",
                    safe_mundane=outcome.safe_mundane,
                    observable_outcome=outcome.observable_outcome,
                    transition=transition,
                ),
                (target.id, None),
                False,
            )

        # A new place: frozen intent selected it, the resolver let it auto-succeed, and its name and
        # containment come from the generated profile, never from the player's inflected wording.
        destination = outcome.destination
        if destination is None:
            raise PlanContractError("new destination lacks a generated name and durable profile")
        name = " ".join(destination.name.split())
        # Containment is the binder's typed choice of a known place (new_in:<ID>), else none.
        parent = getattr(by_id.get(action.destination_parent_location_id), "canonical_name", None)
        transition = SceneTransitionPlan(
            required=True,
            transition_type="location_transition",
            destination_location=name,
            destination_parent_location=parent,
            destination_resident_role=" ".join((destination.resident_role or "").split()) or None,
            reason=action.intent,
            bridge_summary=(
                "DESTINATION PROFILE: "
                + " ".join(destination.profile.split())
                + "\nTRANSITION: "
                + (outcome.observable_outcome or action.intent)
            ),
        )
        return (
            ActionStepPlan(
                action_type="movement",
                intent=action.intent,
                resolution="auto_success",
                safe_mundane=outcome.safe_mundane,
                observable_outcome=outcome.observable_outcome,
                transition=transition,
            ),
            (location_id, (name, parent)),
            True,
        )

    @staticmethod
    def _compile_nonmovement(action: PlayerActionIntent, outcome) -> ActionStepPlan:
        request = action.requested_inventory
        if request is not None and outcome.npc_response != "refused":
            return ActionStepPlan(
                action_type="inventory", intent=action.intent,
                resolution=outcome.resolution, safe_mundane=outcome.safe_mundane,
                observable_outcome=outcome.observable_outcome,
                blocking_reason=outcome.blocking_reason, public_blocking_reason=outcome.blocking_reason,
                item_id=request.item_id, inventory_operation=request.operation,
                inventory_actor_id=request.actor_id, inventory_target_id=request.target_id,
                item_placement_detail=request.placement_detail,
            )
        if request is not None and outcome.resolution == "auto_success":
            raise PlanContractError("An addressed physical action cannot succeed while its actor refuses")
        transition = SceneTransitionPlan()
        if (
            action.action_type in {"rest", "wait"}
            and outcome.resolution == "auto_success"
            and (action.elapsed_time or action.time_after)
        ):
            transition = SceneTransitionPlan(
                required=True,
                transition_type="time_transition",
                elapsed_time=action.elapsed_time,
                time_after=action.time_after,
                reason=action.intent,
            )
        # A success without a written result still happened; the narrator describes it.
        return ActionStepPlan(
            action_type=action.action_type,
            intent=action.intent,
            resolution=outcome.resolution,
            safe_mundane=outcome.safe_mundane,
            observable_outcome=outcome.observable_outcome,
            public_blocking_reason=outcome.blocking_reason,
            blocking_reason=outcome.blocking_reason,
            item_id=action.item_id,
            inventory_operation=action.inventory_operation,
            inventory_target_id=action.inventory_target_id,
            item_placement_detail=action.item_placement_detail,
            transition=transition,
        )

    async def compile(
        self,
        campaign_id: UUID,
        contract: PlayerIntentContract,
        decision: TurnOutcomeDecision,
        *,
        player_input: str | None = None,
    ) -> CoordinatedTurnPlan:
        _scene_id, state, locations = await self._world(campaign_id)
        by_id = {item.id: item for item in locations}
        outcome_by_index = self._outcome_map(contract, decision)
        cursor = (state.location_id, None)
        steps: list[ActionStepPlan] = []
        discovery_steps: list[int] = []
        introductions = [item.model_copy(deep=True) for item in decision.item_introductions]
        created_by_source = {}
        if any(item.after_action_index is not None for item in introductions):
            known = (await self._session.execute(select(Entity).where(
                Entity.campaign_id == str(campaign_id), Entity.entity_type == "item"
            ))).scalars().all()
            occupied = {identity_key(name) for entity in known for name in (
                entity.canonical_name, *json.loads(entity.aliases or "[]"))}
            for introduction in introductions:
                source = introduction.after_action_index
                if source is None:
                    continue
                if source >= len(contract.actions):
                    raise PlanContractError("Item creation refers to a nonexistent source action")
                if outcome_by_index[source].resolution != "auto_success":
                    continue
                key = identity_key(introduction.canonical_name)
                if key in occupied:
                    raise PlanContractError("A new artifact requires an identity distinct from existing items")
                occupied.add(key)
                introduction.reserved_item_id = uuid4()
                created_by_source.setdefault(source, []).append(introduction)

        def produced_identity(reference, current_index, ordinal=None):
            artifacts = created_by_source.get(reference, ())
            if reference >= current_index or not artifacts or (ordinal is None and len(artifacts) != 1):
                raise PlanContractError("A produced item reference needs one distinct successful earlier creation")
            ordinal = 0 if ordinal is None else ordinal
            if ordinal >= len(artifacts):
                raise PlanContractError("A produced item ordinal has no corresponding artifact introduction")
            return artifacts[ordinal].reserved_item_id

        for index, action in enumerate(contract.actions):
            effective_action = action
            produced_reference = (action.produced_item_action_index if action.produced_item_action_index is not None
                                  else action.requested_inventory.produced_item_action_index
                                  if action.requested_inventory else None)
            if (produced_reference is not None and produced_reference < index
                    and outcome_by_index[produced_reference].resolution != "auto_success"):
                steps.append(ActionStepPlan(action_type="inventory", intent=action.intent,
                    source_action_index=index, resolution="blocked",
                    blocking_reason="Предмет не создан предыдущим действием.",
                    public_blocking_reason="Предмет не создан предыдущим действием."))
                continue
            if action.produced_item_action_index is not None:
                effective_action = action.model_copy(update={
                    "item_id": produced_identity(action.produced_item_action_index, index, action.produced_item_ordinal),
                    "produced_item_action_index": None,
                    "produced_item_ordinal": None,
                })
            if action.requested_inventory and action.requested_inventory.produced_item_action_index is not None:
                request = action.requested_inventory
                effective_action = action.model_copy(update={"requested_inventory": request.model_copy(update={
                    "item_id": produced_identity(request.produced_item_action_index, index, request.produced_item_ordinal),
                    "produced_item_action_index": None,
                    "produced_item_ordinal": None,
                })})
            outcome = outcome_by_index[index]
            if outcome.resolution == "auto_success":
                if action.expected_effect == "item_creation" and not any(
                    item.after_action_index == index for item in decision.item_introductions
                ):
                    raise PlanContractError("Successful artifact creation is missing its typed item introduction")
                if action.expected_effect == "item_change" and not any(
                    change.action_index == index for change in decision.item_property_changes
                ):
                    raise PlanContractError("Successful physical change is missing its typed property effect")
            if action.action_type == "movement":
                step, next_cursor, discovery = await self._compile_movement(
                    campaign_id=campaign_id,
                    action=action,
                    outcome=outcome,
                    cursor=cursor,
                    by_id=by_id,
                )
                # Only a successful compiled hop advances virtual topology. A blocked hop leaves the
                # cursor at its prior place, so the frozen tail stays structurally valid even though
                # the executor will later record it as skipped.
                if step.resolution == "auto_success":
                    step.transition.carry_participants = list(outcome.carry_participants)
                    cursor = next_cursor
                if discovery and step.resolution == "auto_success":
                    discovery_steps.append(index)
            else:
                step = self._compile_nonmovement(effective_action, outcome)
            step.item_creations = list(created_by_source.get(index, ()))
            step.source_action_index = index
            steps.append(step)

        if decision.item_property_changes:
            registered = {state.item_id for state in await load_item_states(self._session, campaign_id)}
            seen = set()
            for change in decision.item_property_changes:
                if change.action_index >= len(contract.actions):
                    raise PlanContractError("Property effect refers to a nonexistent frozen action")
                source = contract.actions[change.action_index]
                step = steps[change.action_index]
                subject = source.subject_item_id or source.item_id
                key = (change.action_index, change.item_id, change.property_name)
                if (key in seen or change.item_id not in registered or subject != change.item_id
                        or step.resolution != "auto_success"
                        or source.action_type in {"movement", "wait", "rest"}
                        or change.property_name in ItemStateSnapshot.model_fields):
                    raise PlanContractError("Physical property effect requires a distinct successful act on its frozen item")
                step.item_property_changes.append(change)
                seen.add(key)

        # NPC actions are external outcomes, not player-owned intents. A willing
        # local giver may transfer an existing registered item only via an
        # executed receipt; never via an asynchronous Scribe suggestion.
        deferred_effects: list[ActionStepPlan] = []

        def existing_inventory(item_id, source_index):
            return next((step for step in steps if step.item_id == item_id
                         and step.action_type == "inventory"
                         and source_index in {None, step.source_action_index}), None)

        for handoff in decision.npc_item_handoffs:
            # A player's take and a willing keeper's handoff are the same
            # physical act from opposite sides. Compile that existing step as
            # receive, rather than discarding consent and trying to take an owned item.
            receiving_step = existing_inventory(handoff.item_id, handoff.after_action_index)
            # The typed handoff is the resolver's commitment, not a second
            # quotation of its prose summary. Identity, prerequisites and actual
            # possession/presence are checked structurally, never by text equality.
            if handoff.after_action_index is not None:
                prerequisite = next(
                    (row for row in decision.action_outcomes
                     if row.action_index == handoff.after_action_index),
                    None,
                )
                if prerequisite is None or prerequisite.resolution != "auto_success":
                    raise PlanContractError("NPC item handoff depends on an unfinished action")
                if prerequisite.npc_response == "refused":
                    raise PlanContractError("NPC item handoff lacks an approved response")
            if receiving_step is not None:
                if receiving_step.resolution != "auto_success":
                    raise PlanContractError("NPC handoff contradicts an uncompleted inventory act")
                receiving_step.inventory_operation = "receive"
                receiving_step.inventory_actor_id = None
                receiving_step.inventory_target_id = handoff.giver_id
                receiving_step.item_placement_detail = None
                receiving_step.observable_outcome = handoff.evidence
                continue
            if len(steps) + len(deferred_effects) >= 16:
                raise PlanContractError("Too many actions for a typed NPC item handoff")
            deferred_effects.append(ActionStepPlan(
                source_action_index=handoff.after_action_index,
                action_type="inventory",
                intent="NPC voluntarily transfers an existing item to the player",
                resolution="auto_success",
                observable_outcome=handoff.evidence,
                item_id=handoff.item_id,
                inventory_operation="receive",
                inventory_target_id=handoff.giver_id,
            ))

        if decision.item_revelations:
            item_states = {item.item_id: item for item in await load_item_states(self._session, campaign_id)}
            used_sources = set()
            reading_results = {}
            turn_contents = {item.item_id: item.revealed_content for item in item_states.values()
                             if item.revealed_content is not None}
            for revelation in decision.item_revelations:
                key = (revelation.action_index, revelation.item_id)
                if revelation.action_index >= len(contract.actions) or key in used_sources:
                    raise PlanContractError("A revelation must identify one distinct source action")
                step = steps[revelation.action_index]
                item = item_states.get(revelation.item_id)
                subject = contract.actions[revelation.action_index].subject_item_id
                if subject is not None and subject != revelation.item_id:
                    raise PlanContractError("A revelation cannot change the frozen action's subject item")
                if item is None or step.resolution != "auto_success" or step.action_type in {"movement", "inventory", "wait", "rest"}:
                    raise PlanContractError("Item content requires a successful reading/inspection action")
                content = turn_contents.setdefault(item.item_id, revelation.content)
                step.item_revelations.append(revelation.model_copy(update={"content": content}))
                # Canonical content owns this result, even if a model paraphrased
                # another document into its redundant observable_outcome.
                reading_results.setdefault(revelation.action_index, []).append(f"{item.name}: {content}")
                used_sources.add(key)
            for index, results in reading_results.items():
                steps[index].observable_outcome = "\n\n".join(results)

        for handoff in decision.player_item_handoffs:
            offer_source = player_input if player_input is not None else contract.summary
            if handoff.offer_evidence not in offer_source or contract.pending_player_choice:
                raise PlanContractError("Player item handoff lacks a committed player offer")
            if handoff.after_action_index is not None:
                prerequisite = outcome_by_index.get(handoff.after_action_index)
                if prerequisite is None or prerequisite.resolution != "auto_success":
                    raise PlanContractError("Player item handoff depends on an unfinished action")
                if prerequisite.npc_response == "refused":
                    raise PlanContractError("Player item handoff contradicts the recipient's refusal")
            giving_step = existing_inventory(handoff.item_id, handoff.after_action_index)
            if giving_step is not None:
                if giving_step.resolution != "auto_success":
                    raise PlanContractError("Accepted offer contradicts an uncompleted inventory act")
                giving_step.inventory_operation = "give"
                giving_step.inventory_actor_id = None
                giving_step.inventory_target_id = handoff.recipient_id
                giving_step.observable_outcome = handoff.evidence
                continue
            if len(steps) + len(deferred_effects) >= 16:
                raise PlanContractError("Too many actions for a typed player item handoff")
            deferred_effects.append(ActionStepPlan(
                source_action_index=handoff.after_action_index,
                action_type="inventory", intent="Передать предложенный предмет согласившемуся NPC",
                resolution="auto_success", observable_outcome=handoff.evidence,
                item_id=handoff.item_id, inventory_operation="give",
                inventory_target_id=handoff.recipient_id,
            ))

        # Effects happen at their originating act, before a subsequent journey.
        # Frozen action identity is independent of the number of executable receipts.
        if deferred_effects:
            expanded = []
            for step in steps:
                linked = [effect for effect in deferred_effects
                          if effect.source_action_index == step.source_action_index]
                if linked and step.action_type == "service":
                    step.observable_outcome = "Согласие на передачу получено."  # physical results belong to receipts below
                expanded.extend([step, *linked])
            expanded.extend(effect for effect in deferred_effects if effect.source_action_index is None)
            steps = expanded
            discovery_steps = [index for index, step in enumerate(steps)
                               if step.action_type == "movement" and step.source_action_index in discovery_steps]

        first = next((step for step in steps if step.resolution == "auto_success"), None)
        if contract.time_advance and first is not None and not any(
            step.transition.elapsed_time or step.transition.time_after for step in steps
        ):
            # A typed skip ahead moves the scene clock with the first act that happens.
            if first.transition.required:
                first.transition.time_after = contract.time_advance
            else:
                first.transition = SceneTransitionPlan(
                    required=True, transition_type="time_transition",
                    time_after=contract.time_advance, reason=first.intent,
                )

        npc_introductions = [
            PlannedNpcIntroduction(
                canonical_name=item.canonical_name,
                identity_reference=item.identity_reference,
                role=item.role,
                description=item.description,
                appearance=item.appearance,
                voice=item.voice,
                current_goal=item.current_goal,
                temporary_name=item.temporary_name,
                personal_name_evidence=item.personal_name_evidence,
                reason=item.reason,
                after_action_index=item.after_action_index,
                resident_slot=item.resident_slot,
                arrives=item.arrives,
                sent_for=item.sent_for,
            )
            for item in decision.npc_introductions
        ]

        sequence = ActionSequencePlan(summary=contract.summary, steps=steps)
        plan = CoordinatedTurnPlan(
            player_intent=contract.summary,
            resolution=decision.resolution,
            action_sequence=sequence,
            narration_policy=NarrationPolicy(
                dramatic_mode=decision.dramatic_mode,
                allow_new_complication=decision.allow_new_complication,
                complication_source=decision.complication_source,
                pending_player_choice=contract.pending_player_choice,
                protected_player_decisions=contract.protected_player_decisions,
            ),
            npc_introductions=npc_introductions,
            item_introductions=introductions,
            place_introductions=decision.place_introductions,
            world_move=decision.world_move,
            addressed_response_requested=contract.addressed_response_requested,
            personal_name_revealed=False,
            identity_reveal_requested=contract.identity_reveal_requested,
            response_ownership_reason=(
                "Последний ввод игрока явно ожидает ответ адресата."
                if contract.addressed_response_requested
                else None
            ),
            observable_consequences=decision.observable_consequences,
            character_beats=decision.character_beats,
            addressed_response=decision.addressed_response,
            new_fact_candidates=[],
            narration_guidance=decision.narration_guidance,
        )
        if plan.scene_transition.sequence_payload is not None:
            payload = dict(plan.scene_transition.sequence_payload)
            payload["_authority_source"] = "player_intent_compiler"
            payload["_route_discovery_steps"] = discovery_steps
            plan.scene_transition.sequence_payload = payload
        return plan


__all__ = ["ActionPlanCompiler", "MissingDestinationProfile"]
