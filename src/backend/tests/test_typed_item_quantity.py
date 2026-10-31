"""Countable units are typed state: taking coins out of a purse decrements the purse by ID,
never by its name ("кошель с пятью серебряками"), and compensation returns them."""
from uuid import uuid4

import pytest

from app.db.tables import Entity, Item
from app.models.entity import EntityCreate, EntityType
from app.models.turn_authority import PlannedItemIntroduction, TurnAuthority
from app.db.repositories.entity_repo import EntityRepository
from app.services.item_state_authority import load_item_states
from app.services.turn_outcome_materializer import TurnOutcomeMaterializer

from tests.test_turn_saga_failure_paths import _prepared_world


async def _purse(session, w, quantity=None):
    purse = await EntityRepository(session).create(w.cid, EntityCreate(
        entity_type=EntityType.ITEM, canonical_name="Кошель с пятью серебряками"))
    session.add(Item(entity_id=str(purse.id), current_owner_id=str(w.hero.id), is_unique=True, quantity=quantity))
    await session.flush()
    return purse


def _coin(w, purse, count, before=None):
    return TurnAuthority(
        campaign_id=w.cid, trigger_turn_id=w.turn.id, player_character_id=w.hero.id,
        target_scene_id=w.scene.id, player_input="Кладу серебряк на стойку",
        allowed_new_items=[PlannedItemIntroduction(
            canonical_name="серебряк", evidence="серебряк на стойке", placement_detail="стойка",
            taken_from_item_id=purse.id, quantity=count, source_quantity_before=before)])


@pytest.mark.asyncio
async def test_taking_coins_decrements_the_legacy_purse_by_id_and_rollback_returns_them(file_db):
    async with file_db() as session:
        w = await _prepared_world(session)
        purse = await _purse(session, w)  # legacy save: the count lives only in the name
        done = await TurnOutcomeMaterializer(session).materialize(_coin(w, purse, 1, before=5), source_turn_id=w.turn.id)
        coin_id = str(done.introduced_item_ids[0])
        assert (await session.get(Item, str(purse.id))).quantity == 4
        coin = await session.get(Item, coin_id)
        assert (coin.quantity, coin.split_from_item_id) == (1, str(purse.id))
        states = {s.name: s.quantity for s in await load_item_states(session, w.cid)}
        assert states == {"Кошель с пятью серебряками": 4, "серебряк": 1}
        await TurnOutcomeMaterializer(session).rollback(w.turn.id)
        await session.commit()
    async with file_db() as other:
        assert (await other.get(Item, str(purse.id))).quantity == 5
        assert await other.get(Entity, coin_id) is None


@pytest.mark.asyncio
async def test_known_count_wins_and_a_same_named_second_split_is_a_new_identity(file_db):
    async with file_db() as session:
        w = await _prepared_world(session)
        purse = await _purse(session, w, quantity=4)
        session.add(Entity(id=str(uuid4()), campaign_id=str(w.cid), entity_type="item", canonical_name="серебряк",
                           aliases="[]", status="active", provenance="turn_authority", version=1))
        # A stale model-claimed "before" never overrides the typed count.
        await TurnOutcomeMaterializer(session).materialize(_coin(w, purse, 2, before=5), source_turn_id=w.turn.id)
        assert (await session.get(Item, str(purse.id))).quantity == 2


@pytest.mark.asyncio
async def test_taking_more_than_the_source_holds_fails_closed(file_db):
    async with file_db() as session:
        w = await _prepared_world(session)
        purse = await _purse(session, w, quantity=1)
        with pytest.raises(ValueError):
            await TurnOutcomeMaterializer(session).materialize(_coin(w, purse, 3), source_turn_id=w.turn.id)


def test_a_split_requires_a_positive_count():
    with pytest.raises(ValueError):
        PlannedItemIntroduction(canonical_name="серебряк", evidence="серебряк на стойке", taken_from_item_id=uuid4())
