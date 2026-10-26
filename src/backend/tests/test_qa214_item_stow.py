"""F-32: stowing a carried item changes its exact placement, not ownership."""
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import Campaign, Entity, Item
from app.services.action_sequence_executor import ActionSequenceExecutor


@pytest.mark.asyncio
async def test_stow_letter_in_bag_keeps_player_owner_and_persists_placement(
    db_session: AsyncSession,
):
    cid, hero_id, letter_id = uuid4(), uuid4(), uuid4()
    db_session.add(Campaign(id=str(cid), name="F-32 bag stow",
                            player_character_id=str(hero_id)))
    db_session.add_all([
        Entity(id=str(hero_id), campaign_id=str(cid),
               entity_type="character", canonical_name="Тимон"),
        Entity(id=str(letter_id), campaign_id=str(cid),
               entity_type="item", canonical_name="письмо"),
    ])
    db_session.add(Item(entity_id=str(letter_id), current_owner_id=str(hero_id)))
    await db_session.flush()

    receipt = SimpleNamespace()
    step = SimpleNamespace(
        item_id=letter_id, inventory_operation="reposition",
        item_placement_detail="в сумке", inventory_target_id=None,
    )
    await ActionSequenceExecutor(db_session)._apply_inventory_step(
        cid, None, uuid4(), step, receipt,
    )
    item = await db_session.get(Item, str(letter_id))
    assert item.current_owner_id == str(hero_id)
    assert item.current_location_id is None
    assert item.placement_detail == "в сумке"
    assert receipt.item_previous_owner_id == str(hero_id)
    assert receipt.item_result_owner_id == str(hero_id)
    assert receipt.item_result_placement_detail == "в сумке"
