from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.location_repo import LocationRepository
from app.db.tables import Campaign
from app.models.player_intent import PlayerActionIntent, PlayerIntentContract, TurnOutcomeDecision
from app.models.turn_authority import PlannedNpcIntroduction
from app.services.location_identity import display_location_name, same_location_reference
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
    def _location_matches(destination: str, location) -> bool:
        needles = (destination, display_location_name(destination))
        candidates = (
            location.canonical_name,
            display_location_name(location.canonical_name),
            *location.aliases,
        )
        return any(
            same_location_reference(needle, candidate)
            for needle in needles
            for candidate in candidates
            if needle and candidate
        )

    @classmethod
    def _matching_locations(cls, destination: str, locations) -> list:
        return [item for item in locations if cls._location_matches(destination, item)]

    @classmethod
    def _matching_exits(cls, destination: str, exits, by_id: dict[UUID, object]) -> list:
        result = []
        for exit_row in exits:
            target = by_id.get(exit_row.to_location_id)
            candidates = [exit_row.to_location_name, exit_row.label]
            if target is not None:
                candidates.extend(
                    [target.canonical_name, display_location_name(target.canonical_name), *target.aliases]
                )
            if any(
                same_location_reference(needle, candidate)
                for needle in (destination, display_location_name(destination))
                for candidate in candidates
                if needle and candidate
            ):
                result.append((exit_row, target))
        return result

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

    async def missing_destination_profiles(
        self,
        campaign_id: UUID,
        contract: PlayerIntentContract,
        decision: TurnOutcomeDecision,
    ) -> list[MissingDestinationProfile]:
        """Profiles are enrichment of successful explicit new destinations, never route authority."""
        _scene_id, _state, locations = await self._world(campaign_id)
        outcome_by_index = self._outcome_map(contract, decision)
        missing: list[MissingDestinationProfile] = []
        for index, action in enumerate(contract.actions):
            if action.action_type != "movement" or not action.allow_route_discovery:
                continue
            outcome = outcome_by_index[index]
            if outcome.resolution != "auto_success":
                continue
            destination = " ".join(str(action.destination_location or "").split())
            if not destination or self._matching_locations(destination, locations):
                continue
            profile = " ".join(str(outcome.destination_profile or "").split())
            if len(profile) < 80:
                missing.append(MissingDestinationProfile(index, destination))
        return missing

    async def _compile_movement(
        self,
        *,
        campaign_id: UUID,
        action_index: int,
        action: PlayerActionIntent,
        outcome,
        current_location_id: UUID | None,
        locations,
        by_id: dict[UUID, object],
    ) -> tuple[ActionStepPlan, UUID | None, bool]:
        destination = " ".join(str(action.destination_location or "").split())
        if outcome.resolution != "auto_success":
            return (
                ActionStepPlan(
                    action_type="movement",
                    intent=action.intent,
                    resolution=outcome.resolution,
                    safe_mundane=False,
                    observable_outcome=outcome.observable_outcome,
                    blocking_reason=outcome.blocking_reason,
                ),
                current_location_id,
                False,
            )
        if current_location_id is None:
            return (
                ActionStepPlan(
                    action_type="movement",
                    intent=action.intent,
                    resolution="blocked",
                    safe_mundane=False,
                    blocking_reason="Current physical location is unavailable for route compilation.",
                ),
                current_location_id,
                False,
            )

        current = by_id.get(current_location_id)
        if current is not None and self._location_matches(destination, current):
            return (
                ActionStepPlan(
                    action_type="movement",
                    intent=action.intent,
                    resolution="blocked",
                    safe_mundane=False,
                    blocking_reason="Destination resolves to the current physical location.",
                ),
                current_location_id,
                False,
            )

        exits = await self._state.list_exits(
            campaign_id,
            current_location_id,
            include_hidden=True,
        )
        matched_exits = self._matching_exits(destination, exits, by_id)
        if len(matched_exits) > 1:
            return (
                ActionStepPlan(
                    action_type="movement",
                    intent=action.intent,
                    resolution="blocked",
                    safe_mundane=False,
                    blocking_reason="Destination matches multiple existing routes from the current location.",
                ),
                current_location_id,
                False,
            )
        if len(matched_exits) == 1:
            exit_row, target = matched_exits[0]
            if target is None:
                raise TurnPlanningError("route points to a missing target location")
            if not exit_row.active:
                detail = f" ({exit_row.access_rule})" if exit_row.access_rule else ""
                return (
                    ActionStepPlan(
                        action_type="movement",
                        intent=action.intent,
                        resolution="blocked",
                        safe_mundane=False,
                        blocking_reason="Destination route is currently inactive" + detail,
                    ),
                    current_location_id,
                    False,
                )
            if not exit_row.discovered and not action.allow_route_discovery:
                return (
                    ActionStepPlan(
                        action_type="movement",
                        intent=action.intent,
                        resolution="blocked",
                        safe_mundane=False,
                        blocking_reason="Destination exit has not been discovered.",
                    ),
                    current_location_id,
                    False,
                )
            transition = SceneTransitionPlan(
                required=True,
                transition_type="location_transition",
                destination_location=target.canonical_name,
                reason=action.intent,
                bridge_summary=(
                    (
                        "DESTINATION PROFILE: "
                        + " ".join(str(outcome.destination_profile or "").split())
                        + "\nTRANSITION: "
                        + (outcome.observable_outcome or action.intent)
                    )
                    if outcome.destination_profile
                    else outcome.observable_outcome or action.intent
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
                target.id,
                not exit_row.discovered,
            )

        global_matches = self._matching_locations(destination, locations)
        if len(global_matches) > 1:
            return (
                ActionStepPlan(
                    action_type="movement",
                    intent=action.intent,
                    resolution="blocked",
                    safe_mundane=False,
                    blocking_reason="Destination matches multiple known campaign locations.",
                ),
                current_location_id,
                False,
            )
        if len(global_matches) == 1:
            # A known place with no edge from the virtual current location is a graph blocker. The
            # compiler never invents a shortcut merely because the player named a known location.
            return (
                ActionStepPlan(
                    action_type="movement",
                    intent=action.intent,
                    resolution="blocked",
                    safe_mundane=False,
                    blocking_reason="Destination is not an available exit from the current location.",
                ),
                current_location_id,
                False,
            )

        if not action.allow_route_discovery:
            return (
                ActionStepPlan(
                    action_type="movement",
                    intent=action.intent,
                    resolution="blocked",
                    safe_mundane=False,
                    blocking_reason="Player destination is unresolved; route discovery was not authorized.",
                ),
                current_location_id,
                False,
            )

        profile = " ".join(str(outcome.destination_profile or "").split())
        if len(profile) < 80:
            raise TurnPlanningError(
                f"new destination for action {action_index} lacks a durable profile"
            )
        canonical_destination = display_location_name(destination) or destination
        transition = SceneTransitionPlan(
            required=True,
            transition_type="location_transition",
            destination_location=canonical_destination,
            destination_parent_location=None,
            reason=action.intent,
            bridge_summary=(
                "DESTINATION PROFILE: "
                + profile
                + "\nTRANSITION: "
                + (outcome.observable_outcome or action.intent)
            ),
        )
        # The location does not have an ID until the executor creates it. A later movement in the
        # same turn therefore cannot be safely compiled against its exits; virtual topology becomes
        # unknown until execution materializes the new place.
        return (
            ActionStepPlan(
                action_type="movement",
                intent=action.intent,
                resolution="auto_success",
                safe_mundane=outcome.safe_mundane,
                observable_outcome=outcome.observable_outcome,
                transition=transition,
            ),
            None,
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
        return ActionStepPlan(
            action_type=action.action_type,
            intent=action.intent,
            resolution=outcome.resolution,
            safe_mundane=outcome.safe_mundane,
            observable_outcome=outcome.observable_outcome,
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
        current_location_id = state.location_id
        steps: list[ActionStepPlan] = []
        discovery_steps: list[int] = []

        for index, action in enumerate(contract.actions):
            outcome = outcome_by_index[index]
            if action.action_type == "movement":
                step, next_location_id, discovery = await self._compile_movement(
                    campaign_id=campaign_id,
                    action_index=index,
                    action=action,
                    outcome=outcome,
                    current_location_id=current_location_id,
                    locations=locations,
                    by_id=by_id,
                )
                # Only a successful compiled hop advances virtual topology. A blocked hop leaves the
                # cursor at its prior place, so the frozen tail stays structurally valid even though
                # the executor will later record it as skipped.
                if step.resolution == "auto_success":
                    current_location_id = next_location_id
                if discovery and step.resolution == "auto_success":
                    discovery_steps.append(index)
            else:
                step = self._compile_nonmovement(action, outcome)
            steps.append(step)

        introductions = [
            PlannedNpcIntroduction(
                canonical_name=item.canonical_name,
                role=item.role,
                description=item.description,
                appearance=item.appearance,
                voice=item.voice,
                temporary_name=item.temporary_name,
                personal_name_evidence=item.personal_name_evidence,
                reason=item.reason,
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
