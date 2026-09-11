from __future__ import annotations

from uuid import UUID, uuid4

from app.services.action_plan_compiler import ActionPlanCompiler
from app.services.player_intent_interpreter import PlayerIntentInterpreter
from app.services.role_model_router import ModelRole, RoleModelRouter
from app.services.turn_authority_planner import CoordinatedTurnPlan
from app.services.turn_outcome_resolver import TurnOutcomeResolver
from app.services.turn_planner import ActionSequencePlan, TurnPlanningError

_INSTALLED = False


class TurnIntentPlanningPipeline:
    """Production planning pipeline with one semantic owner per phase.

    Phase 1 freezes the player's voluntary authority. Phase 2 resolves external outcomes without
    changing that authority. Phase 3 compiles executable structure against world state. No phase may
    send a rejected executable plan back through an unconstrained full-plan rewrite loop.
    """

    def __init__(self, session, router: RoleModelRouter):
        self._session = session
        self._router = router
        self._intent = PlayerIntentInterpreter(router)
        self._outcomes = TurnOutcomeResolver(router)
        self._compiler = ActionPlanCompiler(session)

    async def plan(
        self,
        *,
        campaign_id: UUID,
        user_input: str,
        context_messages,
        selection,
    ) -> tuple[CoordinatedTurnPlan, dict]:
        contract = await self._intent.interpret(
            selection,
            context_messages,
            user_input,
        )
        decision = await self._outcomes.resolve(
            selection,
            context_messages,
            user_input,
            contract,
        )
        missing = await self._compiler.missing_destination_profiles(
            campaign_id,
            contract,
            decision,
        )
        if missing:
            decision = await self._outcomes.enrich_destination_profiles(
                selection,
                user_input,
                decision,
                missing,
            )
        plan = await self._compiler.compile(campaign_id, contract, decision)
        return plan, {
            "architecture": "frozen_player_intent_v1",
            "intent_contract": contract.model_dump(mode="json"),
            "outcome_decision": decision.model_dump(mode="json"),
            "intent_audit": list(self._intent.audit),
            "outcome_audit": list(self._outcomes.audit),
        }


def install() -> None:
    """Install the new production owner while legacy Planner remains available for compatibility.

    This is migration wiring, not another semantic guard. Once the old planner contracts have been
    retired, TurnSaga can depend on TurnIntentPlanningPipeline directly and this installer disappears.
    """
    global _INSTALLED
    if _INSTALLED:
        return

    from app.db.action_sequence_table import ActionSequence
    from app.db.repositories.scene_repo import SceneRepository
    from app.db.scene_transition_table import SceneTransition
    from app.services.action_sequence_executor import ActionSequenceExecutor
    from app.services.scene_transition_executor import SceneTransitionExecutor
    from app.services.turn_saga import TurnSaga

    original_plan = TurnSaga._plan
    original_apply_action_sequence = SceneTransitionExecutor._apply_action_sequence

    async def intent_ir_plan(
        self,
        *,
        campaign_id,
        user_input,
        messages,
        role_router,
        primary_config,
    ):
        selection = await role_router.resolve(
            campaign_id,
            ModelRole.PLANNER,
            primary_config,
        )
        if selection is None:
            return await original_plan(
                self,
                campaign_id=campaign_id,
                user_input=user_input,
                messages=messages,
                role_router=role_router,
                primary_config=primary_config,
            )
        pipeline = TurnIntentPlanningPipeline(self._session, role_router)
        try:
            plan, audit = await pipeline.plan(
                campaign_id=campaign_id,
                user_input=user_input,
                context_messages=messages,
                selection=selection,
            )
            # The strangler install replaces TurnSaga._plan after compatibility guards were
            # installed. Re-assert the gameplay-only location-card invariant at the new production
            # boundary so a malformed/precompiled plan still cannot create a label-only Location.
            # Keep this above SceneTransitionExecutor: admin/replay callers intentionally retain
            # their low-level semantics.
            from app.services.location_profile_guard import _require_gameplay_profiles

            await _require_gameplay_profiles(self._session, campaign_id, plan)
            return plan, {
                "status": "completed",
                "model_name": selection.config.model_name,
                "model_source": selection.source,
                "plan": plan.model_dump(mode="json"),
                "telemetry": audit,
            }
        except TurnPlanningError as exc:
            # Preserve TurnSaga's established fail-closed semantics. The conservative fallback is
            # non-mutating and cannot smuggle a partially compiled action into execution.
            fallback = CoordinatedTurnPlan.conservative_fallback(user_input)
            return fallback, {
                "status": "fallback",
                "reason": "intent_pipeline_failed",
                "error": str(exc)[:2000],
                "plan": fallback.model_dump(mode="json"),
                "telemetry": {
                    "architecture": "frozen_player_intent_v1",
                    "status": "failed",
                },
            }

    async def compiled_apply_action_sequence(
        self,
        campaign_id,
        source_scene_id,
        trigger_turn_id,
        plan,
    ):
        raw = plan.sequence_payload if isinstance(plan.sequence_payload, dict) else {}
        if raw.get("_authority_source") != "player_intent_compiler":
            return await original_apply_action_sequence(
                self,
                campaign_id,
                source_scene_id,
                trigger_turn_id,
                plan,
            )

        # Reserved compiler metadata is read before ActionSequencePlan intentionally ignores unknown
        # fields. Existing routes need no second NLP pass. Raw human text is consulted only when an
        # explicit unknown destination is about to create new topology, as an independent provenance
        # safety gate rather than a route interpreter.
        discovery_steps = {
            int(value)
            for value in (raw.get("_route_discovery_steps") or [])
            if isinstance(value, int) or str(value).isdigit()
        }
        sequence_plan = ActionSequencePlan.model_validate(raw)
        route_discovery_turn_id = trigger_turn_id if discovery_steps else None
        execution = await ActionSequenceExecutor(self._session).execute(
            campaign_id,
            source_scene_id,
            trigger_turn_id,
            sequence_plan,
            route_discovery_turn_id=route_discovery_turn_id,
        )
        if not execution.final_scene_id:
            raise ValueError("Compiled action sequence has no final scene")
        target_scene = await self._scenes.get_by_id(execution.final_scene_id)
        if not target_scene:
            raise ValueError("Compiled action sequence final scene not found")

        source_location_id = (
            await self._scenes.get_location_id(source_scene_id)
            if source_scene_id
            else None
        )
        target_location_id = await self._scenes.get_location_id(execution.final_scene_id)
        row = SceneTransition(
            id=str(uuid4()),
            campaign_id=str(campaign_id),
            source_scene_id=(str(source_scene_id) if source_scene_id else None),
            target_scene_id=str(execution.final_scene_id),
            trigger_turn_id=str(trigger_turn_id),
            transition_type="action_sequence",
            status="prepared",
            source_location_id=(str(source_location_id) if source_location_id else None),
            target_location_id=(str(target_location_id) if target_location_id else None),
            elapsed_time=None,
            time_after=None,
            reason=sequence_plan.summary or "Frozen player intent action sequence",
            detector="compound_action_executor",
        )
        self._session.add(row)
        await self._session.flush()
        plan.execution_report = execution.model_dump(mode="json")
        return self._to_applied(row, target_scene, execution)

    TurnSaga._plan = intent_ir_plan
    SceneTransitionExecutor._apply_action_sequence = compiled_apply_action_sequence
    _INSTALLED = True


__all__ = ["TurnIntentPlanningPipeline", "install"]
