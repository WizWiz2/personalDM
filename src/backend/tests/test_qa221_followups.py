from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.db.repositories.turn_repo import TurnRepository
from app.db.tables import Item
from app.models.player_intent import (
    ActionOutcomeDecision,
    NpcItemHandoff,
    PlayerActionIntent,
    PlayerIntentContract,
    TurnOutcomeDecision,
)
from app.models.session_zero import SessionZeroUpdate
from app.models.session_zero_interview import SessionZeroInterviewDraft
from app.models.turn import TurnCreate
from app.services.action_plan_compiler import ActionPlanCompiler
from app.services.action_sequence_executor import ActionSequenceExecutor
from app.services.outcome_fact_authority import completed_world_outcomes
from app.services.playable_bootstrap import PlayableBootstrapService
from app.services.player_intent_interpreter import (
    PlayerActionIntentDraft,
    PlayerIntentContractDraft,
    PlayerIntentInterpreter,
    normalize_intent_draft,
)
from app.services.session_zero_service import SessionZeroService
from app.services.starter_item_custody import reconcile_starter_item_custody
from app.services.turn_planner import ActionSequencePlan, ActionStepPlan
from tests.test_qa219_regressions import _world

pytestmark = pytest.mark.interagent_contract_enforced


@pytest.mark.asyncio
@pytest.mark.parametrize("placement", ["В поднятой руке на уровне глаз", "Между пальцами лицевой стороной вверх", "В сумке"])
async def test_held_item_reposition_is_not_limited_to_container_words(db_session, placement):
    cid, _, _, hero, _, letter, scene = await _world(db_session)
    item = await db_session.get(Item, str(letter))
    item.current_owner_id, item.current_location_id = str(hero), None
    item.placement_detail = "В руке"
    await db_session.flush()
    user = await TurnRepository(db_session).create(cid, TurnCreate(role="user", content="Поднимаю письмо перед собой.", scene_id=scene))
    executor = ActionSequenceExecutor(db_session)
    report = await executor.execute(cid, scene, user.id, ActionSequencePlan(steps=[
        ActionStepPlan(action_type="inventory", intent=user.content, resolution="auto_success",
                       item_id=letter, inventory_operation="reposition", item_placement_detail=placement),
    ]))
    assert report.steps[0].status == "completed"
    assert item.current_owner_id == str(hero) and item.current_location_id is None
    assert item.placement_detail == placement
    await executor.rollback_prepared(report.sequence_id)
    assert item.placement_detail == "В руке" and item.current_owner_id == str(hero)


@pytest.mark.asyncio
async def test_offscene_item_reposition_stays_blocked(db_session):
    cid, _, elsewhere, _, _, letter, scene = await _world(db_session)
    item = await db_session.get(Item, str(letter))
    item.current_owner_id, item.current_location_id = None, str(elsewhere)
    await db_session.flush()
    user = await TurnRepository(db_session).create(cid, TurnCreate(role="user", content="Перекладываю письмо.", scene_id=scene))
    report = await ActionSequenceExecutor(db_session).execute(cid, scene, user.id, ActionSequencePlan(steps=[
        ActionStepPlan(action_type="inventory", intent=user.content, resolution="auto_success",
                       item_id=letter, inventory_operation="reposition", item_placement_detail="На другом краю"),
    ]))
    assert report.steps[0].status == "blocked"
    assert item.current_location_id == str(elsewhere)


@pytest.mark.asyncio
@pytest.mark.parametrize("consent", [True, False])
async def test_take_from_keeper_uses_approved_handoff_without_a_second_request(db_session, consent):
    cid, _, _, hero, npc, letter, scene = await _world(db_session)
    text = "Забираю своё письмо, как вы предложили."
    outcome = "Алиса добровольно передаёт письмо Вере."
    contract = PlayerIntentContract(summary=text, actions=[PlayerActionIntent(
        action_type="inventory", intent=text, item_id=letter, inventory_operation="take",
    )])
    decision = TurnOutcomeDecision(action_outcomes=[ActionOutcomeDecision(action_index=0,
        resolution="auto_success", observable_outcome=outcome if consent else "Герой пытается взять письмо.")],
        npc_item_handoffs=[NpcItemHandoff(item_id=letter, giver_id=npc, after_action_index=0,
                                       evidence=outcome)] if consent else [])
    plan = await ActionPlanCompiler(db_session).compile(cid, contract, decision, player_input=text)
    assert len(plan.action_sequence.steps) == 1
    assert plan.action_sequence.steps[0].intent == text
    user = await TurnRepository(db_session).create(cid, TurnCreate(role="user", content=text, scene_id=scene))
    report = await ActionSequenceExecutor(db_session).execute(cid, scene, user.id, plan.action_sequence)
    assert report.steps[0].status == ("completed" if consent else "blocked")
    assert (await db_session.get(Item, str(letter))).current_owner_id == str(hero if consent else npc)


@pytest.mark.asyncio
async def test_imperative_return_with_first_person_purpose_becomes_receive_not_give(db_session):
    cid, _, _, hero, npc, letter, scene = await _world(db_session)
    text = "Алиса, отдай пока письмо — хочу перенести его в другое место."
    draft = PlayerIntentContractDraft(summary=text, actions=[PlayerActionIntentDraft(
        action_type="inventory", intent="Получить письмо обратно", actor_role="speaker",
        item_id=str(letter), inventory_operation="give", inventory_target_id=str(npc),
    )])
    # Even a wrong semantic owner is overridden by the imperative's actual predicate,
    # without conflating it with the later first-person purpose.
    router = SimpleNamespace(generate_json=AsyncMock(return_value={
        "action_ownership": [{"action_index": 0, "actor_role": "speaker", "contribution_kind": "world_action",
            "spatial_effect": "local", "action_type": "inventory", "item_id": str(letter),
            "inventory_operation": "give", "inventory_target_id": str(npc), "evidence_quote": "отдай пока письмо"}],
        "information_request_only": False, "information_recipient": "character", "addressed_character_name": "Алиса",
    }))
    await PlayerIntentInterpreter(router)._review_semantic_ownership(SimpleNamespace(), text, draft, [])
    contract = normalize_intent_draft(draft, text)
    assert contract.actions[0].action_type == "service"
    assert contract.actions[0].inventory_operation is None
    outcome = "Алиса возвращает письмо героине."
    decision = TurnOutcomeDecision(action_outcomes=[ActionOutcomeDecision(action_index=0, resolution="auto_success", observable_outcome=outcome)],
        npc_item_handoffs=[NpcItemHandoff(item_id=letter, giver_id=npc, evidence=outcome, after_action_index=0)])
    plan = await ActionPlanCompiler(db_session).compile(cid, contract, decision, player_input=text)
    user = await TurnRepository(db_session).create(cid, TurnCreate(role="user", content=text, scene_id=scene))
    report = await ActionSequenceExecutor(db_session).execute(cid, scene, user.id, plan.action_sequence)
    assert report.steps[-1].item_operation == "receive"
    assert (await db_session.get(Item, str(letter))).current_owner_id == str(hero)


@pytest.mark.asyncio
async def test_starting_keeper_is_reconciled_before_item_materialization(db_session):
    cid, room, _, hero, _, _, scene = await _world(db_session)
    draft = SessionZeroInterviewDraft.model_validate({"world": {
        "starter_npcs": [{"name": "Алиса", "name_kind": "personal", "role": "хозяйка"}],
        "starter_items": [{"canonical_name": "Ключ от чердака", "initial_placement": "Висит на доске"}],
    }})
    router = SimpleNamespace(generate_json=AsyncMock(return_value={"items": [
        {"item_index": 0, "holder": "npc", "npc_index": 0, "source": "Алиса распоряжается ключом"},
    ]}))
    corrected = await reconcile_starter_item_custody(router, None, None, draft,
        [{"role": "user", "content": "Алиса распоряжается ключом на доске и не разрешает снимать его без согласия."}])
    setup = await SessionZeroService(db_session).ensure_setup(cid)
    setup.status = "draft"
    await SessionZeroService(db_session).update(cid, SessionZeroUpdate(custom_fields={
        "starter_items": [item.model_dump(mode="json") for item in corrected.world.starter_items],
    }))
    result = await PlayableBootstrapService(db_session).ensure(cid, scene, player_character_id=hero,
        starting_location_id=room, starting_situation="Алиса распоряжается ключом на доске.")
    key = await db_session.get(Item, str(result.created_object_id))
    assert key.current_owner_id is not None and key.current_owner_id != str(hero)
    assert key.current_location_id is None
    assert key.placement_detail == "Висит на доске"
    assert draft.world.starter_items[0].held_by_npc is None


@pytest.mark.asyncio
async def test_opening_custody_does_not_invent_a_keeper_for_unowned_scenery():
    draft = SessionZeroInterviewDraft.model_validate({"world": {
        "starter_npcs": [{"name": "Осип Ким", "name_kind": "personal", "role": "клерк"}],
        "starter_items": [{"canonical_name": "Старинный жетон", "initial_placement": "На полу"}],
    }})
    router = SimpleNamespace(generate_json=AsyncMock(return_value={"items": [
        {"item_index": 0, "holder": "unowned", "npc_index": None, "source": "Жетон никому не принадлежит"},
    ]}))
    result = await reconcile_starter_item_custody(router, None, None, draft,
        [{"role": "user", "content": "У входа дежурит Осип. На полу лежит ничейный жетон."}])
    assert result.world.starter_items[0].held_by_npc is None
    assert result.world.starter_items[0].held_by_player is False


@pytest.mark.asyncio
async def test_opening_custody_rejects_an_invented_keeper_index():
    draft = SessionZeroInterviewDraft.model_validate({"world": {
        "starter_items": [{"canonical_name": "Старинный жетон"}],
    }})
    router = SimpleNamespace(generate_json=AsyncMock(return_value={"items": [
        {"item_index": 0, "holder": "npc", "npc_index": 42, "source": "Нет такого NPC"},
    ]}))
    with pytest.raises(ValueError):
        await reconcile_starter_item_custody(router, None, None, draft, [])


@pytest.mark.asyncio
async def test_reading_result_is_a_durable_executed_outcome_not_a_renderer_invention(db_session):
    cid, _, _, _, _, _, scene = await _world(db_session)
    text = "Вскрываю письмо и читаю вслух."
    result = "В письме написано: «Встреча перенесена к утру. Ждите у старого причала»."
    user = await TurnRepository(db_session).create(cid, TurnCreate(role="user", content=text, scene_id=scene))
    await ActionSequenceExecutor(db_session).execute(cid, scene, user.id, ActionSequencePlan(steps=[
        ActionStepPlan(action_type="interaction", intent=text, resolution="auto_success", observable_outcome=result),
    ]))
    assistant = await TurnRepository(db_session).create(cid, TurnCreate(role="assistant", content=result, scene_id=scene, parent_turn_id=user.id))
    assert result in await completed_world_outcomes(db_session, cid, assistant.id)
