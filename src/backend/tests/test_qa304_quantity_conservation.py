"""Coalescing a paid stack must not apply once per subsequent inventory verb."""
import pytest

from app.db.tables import Item
from app.models.item_state import RequestedInventoryAction
from app.models.player_intent import (
    ActionOutcomeDecision,
    PlayerActionIntent,
    PlayerIntentContract,
    TurnOutcomeDecision,
)
from app.services.action_sequence_executor import ActionSequenceExecutor
from app.services.item_state_authority import load_item_states
from tests.test_coin_merge_back import _item
from tests.test_qa219_regressions import _world
from tests.test_qa223_regressions import _execute


@pytest.mark.asyncio
async def test_give_then_store_merges_once_and_undo_restores_all_counts(db_session):
    cid, _, _, hero, npc, _, scene = await _world(db_session)
    purse = await _item(db_session, cid, "Кошель", 2, owner=hero)
    paid = await _item(db_session, cid, "Прошлый платёж", 1, owner=npc, split_from=purse)
    coin = await _item(db_session, cid, "Новый платёж", 2, owner=hero, split_from=purse)
    contract = PlayerIntentContract(summary="Отдаю две монеты. Прошу убрать их в карман.", actions=[
        PlayerActionIntent(action_type="inventory", intent="Отдать монеты.", item_id=coin,
                           inventory_operation="give", inventory_target_id=npc),
        PlayerActionIntent(action_type="service", intent="Убрать монеты.", requested_inventory=RequestedInventoryAction(
            actor_id=npc, item_id=coin, operation="store", placement_detail="в кармане")),
    ])
    decision = TurnOutcomeDecision(action_outcomes=[ActionOutcomeDecision(action_index=i,
        resolution="auto_success", npc_response="accepted", observable_outcome="Монеты приняты.") for i in range(2)])
    execution = await _execute(db_session, cid, scene, contract, decision)
    assert [step.status for step in execution.steps] == ["completed", "completed"]
    states = {str(row.item_id): row.quantity for row in await load_item_states(db_session, cid) if row.quantity is not None}
    assert states == {purse: 2, paid: 3}
    executor = ActionSequenceExecutor(db_session)
    await executor.mark_applied(execution.sequence_id)
    await executor.undo_applied(execution.sequence_id)
    await db_session.commit()
    db_session.expire_all()
    states = {str(row.item_id): row.quantity for row in await load_item_states(db_session, cid) if row.quantity is not None}
    assert states == {purse: 2, paid: 1, coin: 2}
    assert (await db_session.get(Item, coin)).current_owner_id == str(hero)


@pytest.mark.asyncio
async def test_repeated_merge_cannot_spend_a_destroyed_child_again(db_session):
    from app.services.action_item_creation import merge_units

    cid, _, _, hero, _, _, _ = await _world(db_session)
    purse = await _item(db_session, cid, "Кошель", 3, owner=hero)
    coin = await _item(db_session, cid, "Монеты", 2, owner=hero, split_from=purse)
    row = await db_session.get(Item, coin)
    await merge_units(db_session, row)
    await merge_units(db_session, row)
    assert (await db_session.get(Item, purse)).quantity == 5
    states = {str(item.item_id): item.quantity for item in await load_item_states(db_session, cid) if item.quantity is not None}
    assert states == {purse: 5}
