from __future__ import annotations

import json
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.campaign_setup_repo import CampaignSetupRepository
from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.scene_location_table import SceneLocationLink
from app.db.tables import Campaign, Character, Entity, Item, Turn
from app.models.player_intent import PlayerIntentContract, TurnOutcomeDecision
from app.models.turn import ChatMessage
from app.models.turn_authority import PlannedWorldMove
from app.services.action_plan_compiler import ActionPlanCompiler
from app.services.master_director import (
    apply_moves_to_narration_guidance,
    apply_moves_to_outcome_decision,
    outcome_master_block,
)
from app.services.master_service import MasterService
from app.services.player_intent_interpreter import PlayerIntentInterpreter
from app.services.role_model_router import ModelRole, RoleModelRouter
from app.services.turn_authority_planner import CoordinatedTurnPlan
from app.services.turn_outcome_resolver import (
    TurnOutcomeResolver,
    is_pure_ordinary_travel,
    seeks_contact_or_presence,
    solo_physical_presence,
)
from app.services.turn_planner import ActionSequencePlan, PlanContractError, TurnPlanningError
from app.services.world_momentum_service import WorldMomentumService

_INSTALLED = False


def apply_due_deadline(
    decision: TurnOutcomeDecision,
    contract: PlayerIntentContract,
    momentum: dict,
) -> TurnOutcomeDecision:
    """A mutually established deadline expires deterministically at its phase.

    The outcome was frozen in session zero, so no model is authorized to revise
    the consequence. Never erase another successful user outcome to make room.
    """
    due = momentum.get("automatic_deadline")
    if not isinstance(due, dict) or contract.pending_player_choice:
        return decision
    if decision.resolution in {"failure", "uncertain"}:
        return decision
    existing = list(decision.observable_consequences)
    consequence = due["outcome"]
    if consequence not in existing and len(existing) >= 4:
        return decision
    if consequence not in existing:
        existing.append(consequence)
    move = PlannedWorldMove.model_validate(due)
    return decision.model_copy(update={
        "world_move": move,
        "observable_consequences": existing,
        "allow_new_complication": True,
        "complication_source": due["source"],
    })


def _name_forms():
    from app.services.linguistic_intent_analyzer import LinguisticIntentAnalyzer
    pipeline = LinguisticIntentAnalyzer().pipeline
    return lambda name: [{token.text.casefold(), token.lemma_.casefold()} for token in pipeline(name)
                         if not token.is_punct]


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

    async def _destination_cast(self, campaign_id, contract, names) -> dict[str, list[str]]:
        """Typed occupants of every known place the frozen actions travel to."""
        targets = {str(a.destination_location_id) for a in contract.actions if a.destination_location_id}
        if not targets:
            return {}
        campaign = await self._session.get(Campaign, str(campaign_id))
        rows = await self._session.execute(
            select(Character.current_location_id, Entity.canonical_name)
            .join(Entity, Entity.id == Character.entity_id)
            .where(
                Entity.campaign_id == str(campaign_id),
                Character.current_location_id.in_(targets),
                Entity.id != (campaign.player_character_id if campaign else ""),
            )
        )
        cast: dict[str, list[str]] = {}
        for location_id, name in rows.all():
            cast.setdefault(names.get(location_id, location_id), []).append(name)
        return cast

    async def _uncast(self, campaign_id, names) -> tuple[str, ...]:
        """Personal names no character carries yet. Each word matches by surface or UD lemma, so
        a surname alone or an inflected form («Белова») is its bearer (replay16 k3, p2)."""
        if not names or not isinstance(self._session, AsyncSession):
            return ()
        forms = _name_forms()
        known = [forms(name) for name in (await self._session.execute(
            select(Entity.canonical_name).where(Entity.campaign_id == str(campaign_id),
                                                Entity.entity_type == "character"))).scalars()]

        def within(short, long):
            return bool(short) and all(any(word & other for other in long) for word in short)

        return tuple(name for name in names if not any(
            within(parts := forms(name), bearer) or within(bearer, parts) for bearer in known))

    async def _without_carried_offscreen(self, campaign_id, decision):
        """An offscreen person whose personal name a character already carries is that character."""
        named = [npc.canonical_name for npc in decision.npc_introductions if npc.sent_for and not npc.temporary_name]
        fresh = set(await self._uncast(campaign_id, named)) if isinstance(self._session, AsyncSession) else set(named)
        return decision.model_copy(update={"npc_introductions": [
            npc for npc in decision.npc_introductions
            if not npc.sent_for or npc.temporary_name or npc.canonical_name in fresh]})

    async def _location_catalog(self, campaign_id) -> tuple[dict[str, str], dict[str, dict]]:
        """Known places for destination binding; current marks where the player is now."""
        locations = [
            place for place in await LocationRepository(self._session).list_by_campaign(campaign_id)
            if place.status != "inactive"
        ]
        names = {str(location.id): location.canonical_name for location in locations}
        openings = dict((await self._session.execute(
            select(SceneLocationLink.location_id, Turn.content)
            .join(Turn, Turn.scene_id == SceneLocationLink.scene_id)
            .where(Turn.campaign_id == str(campaign_id), Turn.role == "assistant")
            .order_by(Turn.created_at.desc())
        )).all())
        try:
            here = str((await self._compiler._world(campaign_id))[1].location_id)
        except TurnPlanningError:
            here = None
        exit_labels: dict[str, list[str]] = {}
        if here and here != "None":
            for route in await self._compiler._state.list_exits(campaign_id, UUID(here)):
                if route.active and route.discovered:
                    exit_labels.setdefault(str(route.to_location_id), []).append(route.label)
        return names, {
            key: {
                "name": location.canonical_name,
                "parent": names.get(str(location.parent_location_id)),
                "description": (location.description or "")[:240],
                "scene_opening": (openings.get(key) or "")[:240],
                "current": key == here,
                "exit_labels_from_current": exit_labels.get(key, []),
            }
            for location in locations
            for key in [str(location.id)]
        }

    async def plan(
        self,
        *,
        campaign_id: UUID,
        user_input: str,
        context_messages,
        selection,
    ) -> tuple[CoordinatedTurnPlan, dict]:
        names, catalog = await self._location_catalog(campaign_id)
        known_cast = dict((await self._session.execute(
            select(Entity.id, Entity.canonical_name).where(
                Entity.campaign_id == str(campaign_id), Entity.entity_type == "character",
            )
        )).all()) if isinstance(self._session, AsyncSession) else None
        contract = await self._intent.interpret(
            selection,
            context_messages,
            user_input,
            location_references=names,
            location_catalog=catalog,
        )
        # Director moves are selected before outcome resolution so force_introduce_contact
        # can add optional contact guidance to the resolver. Rhythm is NOT persisted here.
        seek_contact = seeks_contact_or_presence(contract)
        empty_cast = solo_physical_presence(context_messages)
        master_service = MasterService(self._session)
        persona, director, rhythm_before = await master_service.select_moves_for_turn(
            campaign_id,
            seek_contact=seek_contact,
            empty_companion_cast=empty_cast,
            persist_rhythm=False,
        )

        # A deterministic pacing signal and verified actor goals are fed into
        # the *outcome* resolver. Narrator prose alone cannot move the world.
        momentum = {"sources": [], "pressure_due": False, "quiet_turns": 0}
        scene_id = None
        if isinstance(self._session, AsyncSession):
            campaign = await self._session.get(Campaign, str(campaign_id))
            scene_id = (
                UUID(campaign.current_scene_id)
                if campaign and campaign.current_scene_id else None
            )
            momentum = await WorldMomentumService(self._session).snapshot(
                campaign_id, scene_id, patience=persona.initiative.patience_turns,
            )
        world_context = context_messages
        if isinstance(self._session, AsyncSession) and world_context:
            setup = await CampaignSetupRepository(self._session).get(campaign_id)
            if setup and setup.status == "completed":
                agreements = json.dumps({"starting_situation": setup.starting_situation,
                                         "world_summary": setup.world_summary}, ensure_ascii=False)
                first = world_context[0]
                world_context = [ChatMessage(role=first.role, content=first.content
                    + "\n[SESSION AGREEMENTS — standing conditions, not current positions]\n"
                    + agreements
                    + "\nEvaluate promised conditions using current knowledge/input. A refusal on an "
                      "earlier request does not erase these terms. Actual inventory and later "
                      "grounded changes remain authoritative; never reset physical state to the opening."),
                    *world_context[1:]]
        # Outcome resolution must know existing NPC-held item identities, or
        # a legitimate handoff cannot produce a verified giver/item receipt.
        # Only physically co-located characters are eligible to give objects.
        if isinstance(self._session, AsyncSession) and scene_id and context_messages:
            location_id = await SceneRepository(self._session).get_location_id(scene_id)
            if location_id:
                held = (await self._session.execute(
                    select(Entity.id, Entity.canonical_name, Item.current_owner_id)
                    .join(Item, Item.entity_id == Entity.id)
                    .join(Character, Character.entity_id == Item.current_owner_id)
                    .where(
                        Entity.campaign_id == str(campaign_id),
                        Character.current_location_id == str(location_id),
                        Item.current_owner_id != str(campaign.player_character_id or ""),
                    )
                )).all()
                if held:
                    giver_ids = {owner_id for _, _, owner_id in held}
                    giver_names = dict((await self._session.execute(
                        select(Entity.id, Entity.canonical_name).where(Entity.id.in_(giver_ids))
                    )).all())
                    evidence = "\n".join(
                        f"- {name} [id={item_id}]; NPC holder: "
                        f"{giver_names.get(owner_id, 'unknown')} [id={owner_id}]"
                        for item_id, name, owner_id in held
                    )
                    first = world_context[0]
                    world_context = [
                        ChatMessage(role=first.role, content=first.content
                            + "\n[NPC-HELD REGISTERED ITEMS — confirmed physical inventory]\n"
                            + evidence
                            + "\nUse these exact ids only after the actual NPC agrees to a handoff."),
                        *world_context[1:],
                    ]
        if momentum["sources"] and world_context:
            first = world_context[0]
            source_context = (
                "\n[ENGINE-VERIFIED ACTIVE WORLD THREADS]\n"
                + json.dumps(momentum, ensure_ascii=False)
                + "\nA world_move names one of these threads by thread_id; its source "
                  "and actor come from the thread. An absent NPC acts only at their "
                  "real location; never bring them into this scene without a "
                  "typed transition. When due_phase is specified, compare it "
                  "with the authoritative world_time_label. A quiet beat may "
                  "still be appropriate where the world has no lawful move."
            )
            world_context = [
                ChatMessage(role=first.role, content=first.content + source_context),
                *world_context[1:],
            ]

        resident_slots = await self._compiler.resident_slots(campaign_id)
        # A due world initiative rides the outcome call even on known-route travel.
        world_due = bool(momentum.get("pressure_due") and momentum.get("sources"))
        decision = None if world_due else await self._compiler.resolve_known_travel(campaign_id, contract)
        outcome_owner = "route_graph" if decision is not None else "external_resolver"
        if decision is None:
            decision = await self._outcomes.resolve(
                selection,
                world_context,
                user_input,
                contract,
                force_introduce_contact=director.forced_introduce_contact,
                resident_slots=resident_slots,
                initiative=(persona.initiative, momentum),
                master_block=outcome_master_block(persona, list(director.moves)),
                destination_cast=await self._destination_cast(campaign_id, contract, names),
                known_places=names,
                known_cast=known_cast,
                required_absent=await self._uncast(campaign_id, contract.named_absent_people),
            )
        elif director.forced_introduce_contact and not decision.npc_introductions:
            # Route-graph travel decisions skip the LLM outcome path; re-enter the resolver so
            # forced contact-seeking gets its (optional) chance at a typed introduction.
            decision = await self._outcomes.resolve(
                selection,
                world_context,
                user_input,
                contract,
                force_introduce_contact=True,
                known_cast=known_cast,
            )
            outcome_owner = "external_resolver_forced_intro"
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

        # Primary: structural authority levers. Secondary: narration_guidance seasoning.
        committed_travel = is_pure_ordinary_travel(contract) or any(
            action.action_type == "movement" for action in contract.actions
        )
        decision = apply_moves_to_outcome_decision(decision, director)
        guidance = apply_moves_to_narration_guidance(
            list(decision.narration_guidance),
            director,
            substance_active=committed_travel,
        )
        decision = decision.model_copy(update={"narration_guidance": guidance})
        decision = apply_due_deadline(decision, contract, momentum)
        decision = await self._without_carried_offscreen(campaign_id, decision)

        for attempt in range(2):
            try:
                plan = await self._compiler.compile(campaign_id, contract, decision, player_input=user_input)
                break
            except PlanContractError as exc:
                if attempt:
                    raise
                # No execution has started. Repair the model's decision, keeping
                # the human's frozen acts and the authoritative world unchanged.
                decision = await self._outcomes.resolve(
                    selection, world_context, user_input, contract,
                    resident_slots=resident_slots,
                    initiative=(persona.initiative, momentum),
                    master_block=outcome_master_block(persona, list(director.moves)),
                    destination_cast=await self._destination_cast(campaign_id, contract, names),
                    known_places=names,
                    known_cast=known_cast,
                    required_absent=await self._uncast(campaign_id, contract.named_absent_people),
                    repair_feedback=str(exc) + "\nPrevious decision:\n" + decision.model_dump_json(),
                )
                decision = apply_due_deadline(apply_moves_to_outcome_decision(decision, director), contract, momentum)
                decision = await self._without_carried_offscreen(campaign_id, decision)
                missing = await self._compiler.missing_destination_profiles(campaign_id, contract, decision)
                if missing:
                    decision = await self._outcomes.enrich_destination_profiles(selection, user_input, decision, missing)
        return plan, {
            "architecture": "frozen_player_intent_v1",
            "outcome_owner": outcome_owner,
            "world_momentum": momentum,
            # Step-coverage telemetry: the compiler is 1:1 with the frozen contract, so any
            # gap between what the player committed and what is planned was created upstream
            # (ownership speech filter, information-request wipe, extraction cap). Surfacing
            # it per turn separates "planner dropped it" from "intent never had it"
            # (playtest 2026-10-12: silent multi-action truncation).
            "action_coverage": {
                "requested_actions": len(contract.actions),
                "planned_steps": len(plan.action_sequence.steps),
            },
            "intent_contract": contract.model_dump(mode="json"),
            "outcome_decision": decision.model_dump(mode="json"),
            "intent_audit": list(self._intent.audit),
            "outcome_audit": list(self._outcomes.audit),
            "game_master": {
                "id": persona.id,
                "display_name": persona.display_name,
                "moves": list(director.moves),
                "forced_introduce_contact": director.forced_introduce_contact,
                "committed_travel": committed_travel,
                "rhythm_pending": True,
                "rhythm_before": rhythm_before.model_dump(mode="json"),
            },
        }


def install() -> None:
    """Install the new production owner while legacy Planner remains available for compatibility.

    This is migration wiring, not another semantic guard. Once the old planner contracts have been
    retired, TurnSaga can depend on TurnIntentPlanningPipeline directly and this installer disappears.
    """
    global _INSTALLED
    if _INSTALLED:
        return

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
            # Do not convert a failed typed pipeline into an empty conservative plan. The strangler
            # installer replaced the older dead-turn wrapper around TurnSaga._plan, so returning a
            # fallback here used to hide the original control-plane defect until TurnAuthority and
            # every live-model case reported the same generic "no concrete typed outcome" error.
            # Preserve fail-closed semantics by surfacing the phase error at the actual boundary.
            error = TurnPlanningError(f"frozen intent pipeline failed: {exc}")
            error.telemetry = getattr(exc, "telemetry", {})
            raise error from exc

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
            await self._scenes.get_location_id(source_scene_id) if source_scene_id else None
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
