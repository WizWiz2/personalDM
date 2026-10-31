"""verify281 T7/T8: units split from a purse rejoin it by ID when they come back to the same custody,
and units paid to the same holder join that holder's earlier units of the same split source.
No lingering 'серебряк' item per payment; compensation restores both counts and identities."""
from uuid import uuid4

import pytest

from app.db.tables import Entity, Item
from app.models.player_intent import ActionOutcomeDecision, PlayerActionIntent, PlayerIntentContract, TurnOutcomeDecision
from app.services.action_sequence_executor import ActionSequenceExecutor
from app.services.item_state_authority import load_item_states
from tests.test_qa219_regressions import _world
from tests.test_qa223_regressions import _execute


async def _item(session, cid, name, quantity, owner=None, location=None, split_from=None):
    item_id = str(uuid4())
    session.add(Entity(id=item_id, campaign_id=str(cid), entity_type="item", canonical_name=name,
                       aliases="[]", status="active", provenance="turn_authority", version=1))
    await session.flush()
    session.add(Item(entity_id=item_id, current_owner_id=owner and str(owner), is_unique=True,
                     current_location_id=location and str(location), quantity=quantity, split_from_item_id=split_from))
    await session.flush()
    return item_id


def _step(item_id, operation, target=None):
    contract = PlayerIntentContract(summary="Монета.", actions=[PlayerActionIntent(
        action_type="inventory", intent="Монета.", item_id=item_id, inventory_operation=operation,
        inventory_target_id=target)])
    return contract, TurnOutcomeDecision(action_outcomes=[ActionOutcomeDecision(
        action_index=0, resolution="auto_success", observable_outcome="Сделано.")])


@pytest.mark.asyncio
async def test_coin_put_back_rejoins_its_purse_by_id_and_undo_splits_it_again(db_session):
    cid, room, _, hero, _, _, scene = await _world(db_session)
    purse = await _item(db_session, cid, "Кошель", 0, owner=hero)
    coin = await _item(db_session, cid, "Серебряк на ладони", 1, location=room, split_from=purse)
    execution = await _execute(db_session, cid, scene, *_step(coin, "store"))
    assert execution.steps[0].status == "completed"
    states = {str(s.item_id): s.quantity for s in await load_item_states(db_session, cid)}
    assert states.get(purse) == 1 and coin not in states
    executor = ActionSequenceExecutor(db_session)
    await executor.mark_applied(execution.sequence_id)
    await executor.undo_applied(execution.sequence_id)
    states = {str(s.item_id): s.quantity for s in await load_item_states(db_session, cid)}
    assert states.get(purse) == 0 and states.get(coin) == 1
    assert (await db_session.get(Item, coin)).current_location_id == str(room)


@pytest.mark.asyncio
async def test_second_payment_joins_the_recipients_earlier_coins_of_the_same_purse(db_session):
    cid, _, _, hero, npc, _, scene = await _world(db_session)
    purse = await _item(db_session, cid, "Кошель", 3, owner=hero)
    paid = await _item(db_session, cid, "серебряк", 1, owner=npc, split_from=purse)
    coin = await _item(db_session, cid, "серебряк", 2, owner=hero, split_from=purse)
    await _execute(db_session, cid, scene, *_step(coin, "give", target=npc))
    states = {str(s.item_id): (s.quantity, str(s.owner_id)) for s in await load_item_states(db_session, cid)}
    assert states.get(paid) == (3, str(npc)) and coin not in states and states[purse][0] == 3
