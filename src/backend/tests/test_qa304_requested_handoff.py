"""A consented request to stow freshly produced units grants custody of those exact units."""
import pytest

from app.db.tables import Item
from app.models.item_state import RequestedInventoryAction
from app.models.player_intent import (
    ActionOutcomeDecision,
    PlayerActionIntent,
    PlayerIntentContract,
    TurnOutcomeDecision,
)
from app.models.turn_authority import PlannedItemIntroduction
from app.services.action_sequence_executor import ActionSequenceExecutor
from app.services.item_state_authority import load_item_states
from tests.test_coin_merge_back import _item
from tests.test_qa219_regressions import _world
from tests.test_qa223_regressions import _execute


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["take", "store"])
async def test_accepted_request_acquires_the_created_units_and_undo_returns_them(db_session, operation):
    cid, _, _, hero, npc, original, scene = await _world(db_session)
    purse = await _item(db_session, cid, "Кошель", 5, owner=hero)
    original_owner = (await db_session.get(Item, str(original))).current_owner_id
    contract = PlayerIntentContract(summary="Отсчитываю две монеты и прошу взять их на хранение.", actions=[
        PlayerActionIntent(action_type="interaction", intent="Отделить две монеты.", expected_effect="item_creation"),
        PlayerActionIntent(action_type="service", intent="Взять монеты на хранение.", requested_inventory=RequestedInventoryAction(
            actor_id=npc, produced_item_action_index=0, produced_item_ordinal=0, operation=operation,
            placement_detail="в кармане" if operation == "store" else None)),
    ])
    decision = TurnOutcomeDecision(action_outcomes=[ActionOutcomeDecision(action_index=i, resolution="auto_success",
        npc_response="accepted" if i else "not_applicable", observable_outcome="Монеты отделены." if not i else "Монеты приняты.")
        for i in range(2)], item_introductions=[PlannedItemIntroduction(canonical_name="Две монеты", after_action_index=0,
            evidence="Отсчитаны две монеты.", owned_by_player=True, quantity=2, taken_from_item_id=purse)])
    execution = await _execute(db_session, cid, scene, contract, decision)
    assert all(step.status == "completed" for step in execution.steps)
    states = [item for item in await load_item_states(db_session, cid) if item.quantity is not None]
    assert sorted((item.quantity, str(item.owner_id)) for item in states) == sorted([(3, str(hero)), (2, str(npc))])
    assert (await db_session.get(Item, str(original))).current_owner_id == original_owner
    executor = ActionSequenceExecutor(db_session)
    await executor.mark_applied(execution.sequence_id)
    await executor.undo_applied(execution.sequence_id)
    await db_session.commit()
    db_session.expire_all()
    states = [item for item in await load_item_states(db_session, cid) if item.quantity is not None]
    assert [(str(item.item_id), item.quantity) for item in states] == [(purse, 5)]


@pytest.mark.asyncio
@pytest.mark.parametrize("substitution", [False, True])
async def test_a_redundant_handoff_cannot_replace_the_requested_item(db_session, substitution):
    from app.models.player_intent import PlayerItemHandoff
    from app.services.action_plan_compiler import ActionPlanCompiler
    from app.services.turn_planner import PlanContractError

    cid, _, _, hero, npc, requested, scene = await _world(db_session)
    original = await db_session.get(Item, str(requested))
    original.current_owner_id, original.current_location_id = str(hero), None
    other = await _item(db_session, cid, "Другая вещь", 1, owner=hero)
    text = "Прошу взять этот документ на хранение."
    contract = PlayerIntentContract(summary=text, actions=[PlayerActionIntent(action_type="service", intent=text,
        requested_inventory=RequestedInventoryAction(actor_id=npc, item_id=requested, operation="store"))])
    decision = TurnOutcomeDecision(action_outcomes=[ActionOutcomeDecision(action_index=0, resolution="auto_success",
        npc_response="accepted", observable_outcome="Документ принят.")], player_item_handoffs=[PlayerItemHandoff(
            item_id=other if substitution else requested, recipient_id=npc, after_action_index=0,
            offer_evidence=text, evidence="Документ принят.")])
    if substitution:
        with pytest.raises(PlanContractError, match="frozen request subject"):
            await ActionPlanCompiler(db_session).compile(cid, contract, decision, player_input=text)
        assert original.current_owner_id == str(hero)
    else:
        execution = await _execute(db_session, cid, scene, contract, decision)
        assert [step.status for step in execution.steps] == ["completed", "completed"]
        assert original.current_owner_id == str(npc)
    assert (await db_session.get(Item, other)).current_owner_id == str(hero)
