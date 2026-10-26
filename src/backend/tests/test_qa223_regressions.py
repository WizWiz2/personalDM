"""Execution, provenance and model-boundary regressions from QA-223."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.db.repositories.scene_repo import SceneRepository
from app.db.repositories.turn_repo import TurnRepository
from app.db.tables import Entity, Item
from app.models.item_state import ItemPositionClaim, ItemPropertyChange, RequestedInventoryAction
from app.models.player_intent import (
    ActionOutcomeDecision,
    NpcItemHandoff,
    PlayerActionIntent,
    PlayerIntentContract,
    PlayerItemHandoff,
    TurnOutcomeDecision,
)
from app.models.scene_state import LocationExitCreate
from app.models.turn import ChatMessage, TurnCreate
from app.models.turn_authority import PlannedItemIntroduction, TurnAuthority
from app.services.action_plan_compiler import ActionPlanCompiler
from app.services.action_sequence_executor import ActionSequenceExecutor
from app.services.item_state_authority import item_position_violations, load_item_states
from app.services.narration_publication_guard import NarrationPublicationGuard
from app.services.player_intent_interpreter import (
    PlayerActionIntentDraft,
    PlayerIntentContractDraft,
    _intent_wire_model,
    normalize_intent_draft,
)
from app.services.scene_state_service import SceneStateService
from app.services.turn_authority_validator import TurnAuthorityValidator, inventory_validation_model
from app.services.turn_intent_pipeline import TurnIntentPlanningPipeline
from app.services.turn_outcome_resolver import _outcome_wire_model
from app.services.turn_planner import PlanContractError
from tests.test_qa219_regressions import _world
from tests.test_qa220_regressions import _auth

pytestmark = pytest.mark.interagent_contract_enforced


async def _execute(session, cid, scene, contract, decision):
    plan = await ActionPlanCompiler(session).compile(cid, contract, decision, player_input=contract.summary)
    user = await TurnRepository(session).create(cid, TurnCreate(role="user", content=contract.summary, scene_id=scene))
    return await ActionSequenceExecutor(session).execute(cid, scene, user.id, plan.action_sequence)


def _success(npc_response="not_applicable"):
    return ActionOutcomeDecision(action_index=0, resolution="auto_success", npc_response=npc_response,
                                 observable_outcome="Действие выполнено.")


@pytest.mark.asyncio
async def test_addressed_storage_preserves_request_and_executes_npc_custody_with_undo(db_session):
    cid, room, _, _, npc, letter, scene = await _world(db_session)
    item = await db_session.get(Item, str(letter))
    item.current_owner_id, item.current_location_id, item.placement_detail = None, str(room), "на стойке"
    await db_session.flush()
    text = "Алиса, уберите документ в свой ящик."
    draft = PlayerIntentContractDraft(summary=text, addressed_character_name="Алиса", actions=[
        PlayerActionIntentDraft(action_type="inventory", actor_role="addressee", requested_actor_id=str(npc),
            intent=text, item_id=str(letter), inventory_operation="store", item_placement_detail="в своём ящике")])
    contract = normalize_intent_draft(draft, text)
    assert contract.actions[0].action_type == "service"
    assert contract.actions[0].requested_inventory.actor_id == npc
    execution = await _execute(db_session, cid, scene, contract, TurnOutcomeDecision(action_outcomes=[_success("accepted")]))
    assert execution.steps[0].item_result_owner_id == npc
    assert item.current_owner_id == str(npc) and item.placement_detail == "в своём ящике"
    executor = ActionSequenceExecutor(db_session)
    await executor.mark_applied(execution.sequence_id)
    await executor.undo_applied(execution.sequence_id)
    assert item.current_owner_id is None and item.current_location_id == str(room)
    assert item.placement_detail == "на стойке"


@pytest.mark.asyncio
async def test_addressed_refusal_cannot_publish_a_successful_inventory_mutation(db_session):
    cid, _, _, _, npc, letter, _ = await _world(db_session)
    contract = PlayerIntentContract(summary="Возьмите документ.", actions=[PlayerActionIntent(
        action_type="service", intent="Возьмите документ.", requested_inventory=RequestedInventoryAction(
            actor_id=npc, item_id=letter, operation="take"))])
    with pytest.raises(PlanContractError, match="refuses"):
        await ActionPlanCompiler(db_session).compile(cid, contract, TurnOutcomeDecision(action_outcomes=[_success("refused")]))


@pytest.mark.asyncio
async def test_player_offer_acceptance_is_not_a_duplicate_prose_substring(db_session):
    cid, _, _, hero, npc, letter, scene = await _world(db_session)
    item = await db_session.get(Item, str(letter))
    item.current_owner_id = str(hero)
    text = "Алиса, держите документ перед собой."
    contract = PlayerIntentContract(summary=text, actions=[PlayerActionIntent(action_type="service", intent=text)])
    decision = TurnOutcomeDecision(action_outcomes=[_success("accepted")], player_item_handoffs=[PlayerItemHandoff(
        item_id=letter, recipient_id=npc, offer_evidence="держите документ перед собой",
        evidence="Алиса принимает предложенную бумагу.", after_action_index=0)])
    execution = await _execute(db_session, cid, scene, contract, decision)
    assert execution.steps[-1].item_operation == "give" and item.current_owner_id == str(npc)


async def _change(session, cid, scene, letter, value):
    contract = PlayerIntentContract(summary="Изменить состояние детали.", actions=[PlayerActionIntent(
        action_type="interaction", intent="Изменить состояние детали.", subject_item_id=letter, expected_effect="item_change")])
    decision = TurnOutcomeDecision(action_outcomes=[_success()], item_property_changes=[ItemPropertyChange(
        item_id=letter, action_index=0, property_name="component.integrity", value=value)])
    return await _execute(session, cid, scene, contract, decision)


@pytest.mark.asyncio
async def test_property_sources_survive_reload_and_compensate_independently(db_session):
    cid, _, _, _, _, letter, scene = await _world(db_session)
    first = await _change(db_session, cid, scene, letter, "open")
    executor = ActionSequenceExecutor(db_session)
    await executor.mark_applied(first.sequence_id)
    await db_session.commit()
    db_session.expire_all()
    assert (await load_item_states(db_session, cid))[0].properties == {"component.integrity": "open"}
    second = await _change(db_session, cid, scene, letter, "repaired")
    assert (await load_item_states(db_session, cid))[0].properties["component.integrity"] == "repaired"
    await executor.rollback_prepared(second.sequence_id)
    assert (await load_item_states(db_session, cid))[0].properties["component.integrity"] == "open"
    await executor.undo_applied(first.sequence_id)
    assert (await load_item_states(db_session, cid))[0].properties == {}


@pytest.mark.asyncio
async def test_remote_property_change_is_blocked_without_a_false_receipt(db_session):
    cid, _, elsewhere, _, _, letter, scene = await _world(db_session)
    item = await db_session.get(Item, str(letter))
    item.current_owner_id, item.current_location_id = None, str(elsewhere)
    execution = await _change(db_session, cid, scene, letter, "broken")
    assert execution.steps[0].status == "blocked" and execution.steps[0].observable_outcome is None
    assert (await load_item_states(db_session, cid))[0].properties == {}


@pytest.mark.asyncio
async def test_a_blocked_source_cannot_leave_an_artifact_or_partial_inventory_mutation(db_session):
    cid, _, elsewhere, _, _, letter, scene = await _world(db_session)
    item = await db_session.get(Item, str(letter))
    item.current_owner_id, item.current_location_id = None, str(elsewhere)
    contract = PlayerIntentContract(summary="Изменить носитель и изготовить результат.", actions=[PlayerActionIntent(
        action_type="interaction", intent="Изменить носитель и изготовить результат.", subject_item_id=letter,
        expected_effect="item_creation")])
    decision = TurnOutcomeDecision(action_outcomes=[_success()], item_property_changes=[ItemPropertyChange(
        item_id=letter, action_index=0, property_name="component.integrity", value="changed")],
        item_introductions=[PlannedItemIntroduction(canonical_name="Побочный результат", after_action_index=0,
            evidence="Заявлен результат недоступного действия.", owned_by_player=True)])
    execution = await _execute(db_session, cid, scene, contract, decision)
    assert execution.steps[0].status == "blocked"
    states = await load_item_states(db_session, cid)
    assert len(states) == 1 and states[0].properties == {}


@pytest.mark.parametrize("kind,expected", [("item_creation", "introduction"), ("item_change", "property effect")])
def test_native_outcome_cannot_assert_success_without_its_required_physical_effect(kind, expected):
    wire = _outcome_wire_model(1, required_effects={0: kind})
    with pytest.raises(ValidationError, match=expected):
        wire.model_validate({"npc_introductions": [], "action_outcomes": [{"action_index": 0,
            "resolution": "auto_success", "observable_outcome": "Физический результат заявлен без записи."}]})


@pytest.mark.asyncio
async def test_compiler_backstop_repairs_missing_artifact_effect_before_execution(db_session):
    cid, _, _, _, _, _, _ = await _world(db_session)
    contract = PlayerIntentContract(summary="Сделать отдельный экземпляр.", actions=[PlayerActionIntent(
        action_type="interaction", intent="Сделать отдельный экземпляр.", expected_effect="item_creation")])
    with pytest.raises(PlanContractError, match="artifact creation"):
        await ActionPlanCompiler(db_session).compile(cid, contract, TurnOutcomeDecision(action_outcomes=[_success()]))


def test_native_inventory_audit_has_required_identity_keys_and_cannot_omit_an_item():
    auth = _auth()
    model = inventory_validation_model(auth)
    schema = model.model_json_schema()
    fields = schema["$defs"]["ItemReviewsByIdentity"]
    assert set(fields["required"]) == {str(item.item_id) for item in auth.item_states}
    with pytest.raises(ValidationError):
        model.model_validate({"verdict": "pass", "item_reviews": {}})


@pytest.mark.asyncio
async def test_keyed_audit_preserves_dialogue_but_does_not_invent_absent_reviews():
    auth = _auth()
    prose = "Осип отвечает: — Я вас услышал."
    reviews = {str(item.item_id): {"item_id": str(item.item_id), "reference": "absent", "claims": []}
               for item in auth.item_states}
    router = SimpleNamespace(generate_json=AsyncMock(return_value={"verdict": "pass", "item_reviews": reviews}))
    result = await TurnAuthorityValidator(router).validate(SimpleNamespace(), auth, prose)
    assert result.verdict == "pass" and router.generate_json.await_count == 1


def test_destination_response_owner_is_not_rejected_against_the_origin_cast():
    wire = _outcome_wire_model(1, present_names=["Герой", "Хранитель"], bound_response_speaker="Хранитель")
    result = wire.model_validate({"npc_introductions": [], "response_speaker_name": "Хранитель",
        "response_after_action_index": 0,
        "action_outcomes": [{"action_index": 0, "resolution": "auto_success", "observable_outcome": "Герой вернулся."}]})
    assert result.response_speaker_name == "Хранитель"


def test_native_addressed_give_can_target_the_controlled_character_without_direction_reversal():
    hero, npc, item = uuid4(), uuid4(), uuid4()
    text = "Алиса, верните документ мне."
    context = [ChatMessage(role="system", content=(
        f"Controlled character: {hero}\nPhysically present characters: Вера [id={hero}], Алиса [id={npc}]\n"
        f"Registered items: документ [id={item}]"))]
    wire = _intent_wire_model(context)
    give = {"evidence_quote": text, "contribution_kind": "world_action", "actor_id": str(npc),
            "intent": text, "action_type": "inventory", "item_id": str(item),
            "inventory_operation": "give", "inventory_target_id": str(hero)}
    payload = {"summary": text, "actions": [give]}
    assert wire.model_validate(payload).actions[0].inventory_target_id == str(hero)
    with pytest.raises(ValidationError):
        wire.model_validate({**payload, "actions": [{**give, "actor_id": str(hero)}]})


@pytest.mark.asyncio
async def test_handoff_executes_before_leaving_its_giver_and_keeps_source_identity(db_session):
    cid, room, elsewhere, hero, npc, letter, scene = await _world(db_session)
    await SceneStateService(db_session).create_exit(cid, room, LocationExitCreate(
        to_location_id=elsewhere, label="в лавку", bidirectional=True))
    contract = PlayerIntentContract(summary="Попросить документ, затем уйти.", actions=[
        PlayerActionIntent(action_type="service", intent="Попросить документ."),
        PlayerActionIntent(action_type="movement", intent="Уйти в лавку.", destination_location="Лавка", destination_location_id=elsewhere),
    ])
    decision = TurnOutcomeDecision(action_outcomes=[_success("accepted"), ActionOutcomeDecision(
        action_index=1, resolution="auto_success", observable_outcome="Герой ушёл в лавку.")],
        npc_item_handoffs=[NpcItemHandoff(item_id=letter, giver_id=npc, after_action_index=0,
            evidence="Алиса отдаёт документ Вере.")])
    execution = await _execute(db_session, cid, scene, contract, decision)
    assert [s.source_action_index for s in execution.steps] == [0, 0, 1]
    assert execution.completed_steps == 3 and execution.steps[1].item_operation == "receive"
    assert execution.steps[1].source_scene_id == scene
    assert (await db_session.get(Item, str(letter))).current_owner_id == str(hero)
    assert await SceneRepository(db_session).get_location_id(execution.final_scene_id) == elsewhere


@pytest.mark.asyncio
async def test_resolved_handoff_wins_over_an_imprecise_requested_placement_label(db_session):
    cid, _, _, hero, npc, letter, scene = await _world(db_session)
    contract = PlayerIntentContract(summary="Алиса, верните документ.", actions=[PlayerActionIntent(
        action_type="service", intent="Верните документ.", requested_inventory=RequestedInventoryAction(
            actor_id=npc, item_id=letter, operation="place", placement_detail="говорящей"))])
    decision = TurnOutcomeDecision(action_outcomes=[_success("accepted")], npc_item_handoffs=[NpcItemHandoff(
        item_id=letter, giver_id=npc, after_action_index=0, evidence="Алиса передаёт документ Вере.")])
    execution = await _execute(db_session, cid, scene, contract, decision)
    assert len(execution.steps) == 1 and execution.steps[0].item_operation == "receive"
    assert execution.steps[0].item_result_owner_id == hero
    assert execution.steps[0].item_result_placement_detail is None


def test_degraded_reading_retains_disclosed_text_without_salvaging_rejected_prose():
    auth = _auth()
    content = "Адресат должен встретиться с автором на пристани. Подпись: Р."
    auth.item_states[0].revealed_content = content
    auth.action_sequence = {"steps": [{"step_index": 0, "action_type": "observation", "status": "completed",
                                      "observable_outcome": "Записка: " + content}]}
    text = NarrationPublicationGuard.render_item_states(auth)
    assert content in text


@pytest.mark.asyncio
async def test_created_artifact_can_be_placed_in_another_location_in_the_same_turn_and_undone(db_session):
    cid, room, elsewhere, hero, _, _, scene = await _world(db_session)
    await SceneStateService(db_session).create_exit(cid, room, LocationExitCreate(
        to_location_id=elsewhere, label="в лавку", bidirectional=True))
    contract = PlayerIntentContract(summary="Сделать оттиск, уйти и выложить его.", actions=[
        PlayerActionIntent(action_type="interaction", intent="Сделать отдельный оттиск.", expected_effect="item_creation"),
        PlayerActionIntent(action_type="movement", intent="Уйти в лавку.", destination_location="Лавка", destination_location_id=elsewhere),
        PlayerActionIntent(action_type="inventory", intent="Выложить новый оттиск на полку.", produced_item_action_index=0,
                           inventory_operation="place", item_placement_detail="на полке"),
    ])
    decision = TurnOutcomeDecision(action_outcomes=[_success(),
        ActionOutcomeDecision(action_index=1, resolution="auto_success", observable_outcome="Герой пришёл в лавку."),
        ActionOutcomeDecision(action_index=2, resolution="auto_success", observable_outcome="Оттиск выложен на полку.")],
        item_introductions=[PlannedItemIntroduction(canonical_name="Оттиск старого знака", after_action_index=0,
            evidence="Герой изготовил отдельный оттиск.", owned_by_player=True)])
    execution = await _execute(db_session, cid, scene, contract, decision)
    produced_id = execution.steps[2].item_id
    assert produced_id is not None and execution.completed_steps == 3
    item = await db_session.get(Item, str(produced_id))
    assert item.current_location_id == str(elsewhere) and item.current_owner_id is None
    assert item.placement_detail == "на полке"
    states = await load_item_states(db_session, cid)
    authority = TurnAuthority(campaign_id=cid, trigger_turn_id=execution.trigger_turn_id,
        player_input=contract.summary, item_states=states, action_sequence=execution.model_dump(mode="json"))
    text = "Созданный оттиск пока находится у героя."
    claim = ItemPositionClaim(item_id=produced_id, evidence=text, scope="executed_action", action_step_index=0,
                              moment="after", mode="owned", owner_id=hero, location_id=room)
    assert not item_position_violations(states, [claim], text, authority.inventory_receipts())
    assert item_position_violations(states, [claim.model_copy(update={"scope": "current"})], text)
    executor = ActionSequenceExecutor(db_session)
    await executor.mark_applied(execution.sequence_id)
    await executor.undo_applied(execution.sequence_id)
    assert await db_session.get(Item, str(produced_id)) is None
    assert await db_session.get(Entity, str(produced_id)) is None


@pytest.mark.asyncio
async def test_failed_creation_skips_dependent_produced_item_act_without_failing_the_turn(db_session):
    cid, _, _, _, _, _, scene = await _world(db_session)
    contract = PlayerIntentContract(summary="Сделать оттиск и положить рядом.", actions=[
        PlayerActionIntent(action_type="interaction", intent="Сделать оттиск.", expected_effect="item_creation"),
        PlayerActionIntent(action_type="inventory", intent="Положить оттиск рядом.", produced_item_action_index=0,
                           inventory_operation="place", item_placement_detail="рядом"),
    ])
    decision = TurnOutcomeDecision(action_outcomes=[ActionOutcomeDecision(action_index=0, resolution="blocked",
        blocking_reason="Материал действительно израсходован."), ActionOutcomeDecision(action_index=1,
            resolution="auto_success", observable_outcome="Недостоверный последующий результат.")])
    execution = await _execute(db_session, cid, scene, contract, decision)
    assert [s.status for s in execution.steps] == ["blocked", "skipped"]
    assert execution.steps[1].observable_outcome is None


@pytest.mark.asyncio
@pytest.mark.product_contract
async def test_production_pipeline_repairs_an_unexecuted_decision_without_rewriting_player_intent(db_session):
    cid, _, _, _, _, letter, _ = await _world(db_session)
    contract = PlayerIntentContract(summary="Изготовить отдельный оттиск.", actions=[PlayerActionIntent(
        action_type="interaction", intent="Изготовить отдельный оттиск.", expected_effect="item_creation")])
    invalid = TurnOutcomeDecision(action_outcomes=[_success()])
    repaired = TurnOutcomeDecision(action_outcomes=[_success()], item_introductions=[PlannedItemIntroduction(
        canonical_name="Новый оттиск", after_action_index=0, evidence="Изготовлен новый отдельный оттиск.", owned_by_player=True)])
    pipeline = TurnIntentPlanningPipeline(db_session, SimpleNamespace())
    pipeline._intent.interpret = AsyncMock(return_value=contract)
    pipeline._outcomes.resolve = AsyncMock(side_effect=[invalid, repaired])
    plan, metadata = await pipeline.plan(campaign_id=cid, user_input=contract.summary,
                                        context_messages=[], selection=SimpleNamespace())
    assert pipeline._outcomes.resolve.await_count == 2
    second = pipeline._outcomes.resolve.call_args_list[1]
    assert second.args[3] is contract and "repair_feedback" in second.kwargs
    assert plan.action_sequence.steps[0].item_creations
    assert metadata["intent_contract"]["summary"] == contract.summary
    assert len(await load_item_states(db_session, cid)) == 1  # planning made no physical mutations
    assert (await db_session.get(Item, str(letter))).current_owner_id is not None
