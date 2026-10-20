"""QA-213 F-35: unrelated undo must not undo an earlier structured item transfer."""
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.action_sequence_table import ActionSequence, ActionStep
from app.db.tables import Campaign, Entity, Item, Turn
from app.services.active_canon_replay import ActiveCanonReplayService
from app.services.initial_world_state import InitialWorldStateService


@pytest.mark.asyncio
async def test_replay_preserves_completed_item_transfer_after_unrelated_undo(
    db_session: AsyncSession,
):
    cid, hero, npc, item_id, transfer_turn = (uuid4() for _ in range(5))
    db_session.add(Campaign(id=str(cid), name="QA-214 item replay"))
    db_session.add_all([
        Entity(id=str(hero), campaign_id=str(cid), entity_type="character", canonical_name="Тимон"),
        Entity(id=str(npc), campaign_id=str(cid), entity_type="character", canonical_name="Ивар"),
        Entity(id=str(item_id), campaign_id=str(cid), entity_type="item", canonical_name="ключ"),
    ])
    db_session.add(Item(entity_id=str(item_id), current_owner_id=str(hero)))
    db_session.add(Turn(
        id=str(transfer_turn), campaign_id=str(cid), role="user",
        status="active", content="Отдаю ключ Ивару.",
    ))
    await db_session.flush()

    # This must be captured before any item mutation.
    await InitialWorldStateService(db_session).ensure_snapshot(cid)
    item = await db_session.get(Item, str(item_id))
    item.current_owner_id = str(npc)
    seq = ActionSequence(
        campaign_id=str(cid), trigger_turn_id=str(transfer_turn),
        status="applied", planned_steps=1, completed_steps=1,
    )
    db_session.add(seq)
    await db_session.flush()
    db_session.add(ActionStep(
        sequence_id=seq.id, step_index=0, action_type="inventory",
        intent="Отдаю ключ", resolution="auto_success", status="completed",
        item_id=str(item_id), item_operation="give",
        item_previous_owner_id=str(hero), item_result_owner_id=str(npc),
    ))
    await db_session.flush()

    # /undo of an unrelated later turn reconstructs all active physical truth.
    await ActiveCanonReplayService(db_session).replay(cid)
    await db_session.refresh(item)
    assert item.current_owner_id == str(npc)

    # If the source turn itself was undone, replay must restore the baseline.
    source = await db_session.get(Turn, str(transfer_turn))
    source.status = "undone"
    seq.status = "undone"
    await db_session.flush()
    await ActiveCanonReplayService(db_session).replay(cid)
    await db_session.refresh(item)
    assert item.current_owner_id == str(hero)
