from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.location_repo import LocationRepository
from app.db.tables import Campaign, Entity
from app.models.player_intent import (
    ActionOutcomeDecision,
    PlayerActionIntent,
    PlayerIntentContract,
    TurnOutcomeDecision,
)
from app.models.turn_authority import PlannedNpcIntroduction
from app.services.scene_state_service import SceneStateService
from app.services.turn_authority_planner import CoordinatedTurnPlan
from app.services.turn_planner import (
    ActionSequencePlan,
    ActionStepPlan,
    NarrationPolicy,
    SceneTransitionPlan,
    TurnPlanningError,
)


@dataclass(frozen=True)
class MissingDestinationProfile:
    action_index: int
    destination: str
    origin: str | None = None
    inside: bool = False  # the hop was bound to its own origin: a new place inside it


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
                raise TurnPlanningError(
                    f"outcome resolver returned duplicate action_index={item.action_index}"
                )
            by_index[item.action_index] = item
        if set(by_index) != expected:
            raise TurnPlanningError(
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
                return None  # bound to its own origin: a new place inside it needs a profile
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
        origin_id, origin = state.location_id, names.get(state.location_id)
        missing: list[MissingDestinationProfile] = []
        for index, action in enumerate(contract.actions):
            if action.action_type != "movement":
                continue
            outcome = outcome_by_index[index]
            inside = origin_id is not None and action.destination_location_id == origin_id
            if (
                outcome.resolution == "auto_success"
                and (action.destination_location_id is None or inside)
                and not outcome.destination
            ):
                missing.append(MissingDestinationProfile(
                    index, action.intent if inside else action.destination_location, origin, inside
                ))
            origin_id = None if inside else action.destination_location_id
            origin = names.get(origin_id, action.destination_location)
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
        if target is not None and not (target.id == location_id and pending is None):
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
            raise TurnPlanningError("new destination lacks a generated name and durable profile")
        name = " ".join(destination.name.split())
        origin, origin_parent = pending or (
            current.canonical_name,
            getattr(by_id.get(current.parent_location_id), "canonical_name", None),
        )
        # A place with no containing place of its own lies inside the one the hop starts from.
        parent = origin if destination.within_current or not origin_parent else origin_parent
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
            transition=transition,
        )

    async def compile(
        self,
        campaign_id: UUID,
        contract: PlayerIntentContract,
        decision: TurnOutcomeDecision,
    ) -> CoordinatedTurnPlan:
        _scene_id, state, locations = await self._world(campaign_id)
        by_id = {item.id: item for item in locations}
        outcome_by_index = self._outcome_map(contract, decision)
        cursor = (state.location_id, None)
        steps: list[ActionStepPlan] = []
        discovery_steps: list[int] = []

        for index, action in enumerate(contract.actions):
            outcome = outcome_by_index[index]
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
                step = self._compile_nonmovement(action, outcome)
            steps.append(step)

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

        introductions = [
            PlannedNpcIntroduction(
                canonical_name=item.canonical_name,
                identity_reference=item.identity_reference,
                role=item.role,
                description=item.description,
                appearance=item.appearance,
                voice=item.voice,
                temporary_name=item.temporary_name,
                personal_name_evidence=item.personal_name_evidence,
                reason=item.reason,
                after_action_index=item.after_action_index,
                resident_slot=item.resident_slot,
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
            npc_introductions=introductions,
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
            canon_constraints=decision.canon_constraints,
            new_fact_candidates=[],
            narration_guidance=decision.narration_guidance,
            ending_hook=decision.ending_hook,
        )
        if plan.scene_transition.sequence_payload is not None:
            payload = dict(plan.scene_transition.sequence_payload)
            payload["_authority_source"] = "player_intent_compiler"
            payload["_route_discovery_steps"] = discovery_steps
            plan.scene_transition.sequence_payload = payload
        return plan


__all__ = ["ActionPlanCompiler", "MissingDestinationProfile"]
