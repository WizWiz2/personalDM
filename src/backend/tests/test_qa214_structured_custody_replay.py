"""F-35: unrelated undo must never revert a committed structured item hand-off."""
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.action_sequence_table import ActionSequence, ActionStep
from app.db.tables import Campaign, Entity, Item, Turn
from app.services.active_canon_replay import ActiveCanonReplayService
from app.services.initial_world_state import InitialWorldStateService


@pytest.mark.asyncio
async def test_unrelated_undo_replays_active_structured_item_transfer(
    db_session: AsyncSession,
):
    cid, hero, recipient, item_id = (uuid4() for _ in range(4))
    user_id, assistant_id, unrelated_id = (uuid4() for _ in range(3))
    db_session.add(Campaign(id=str(cid), name="F-35 custody replay"))
    db_session.add_all([
        Entity(id=str(hero), campaign_id=str(cid), entity_type="character",
               canonical_name="Тимон"),
        Entity(id=str(recipient), campaign_id=str(cid), entity_type="character",
               canonical_name="Ивар"),
        Entity(id=str(item_id), campaign_id=str(cid), entity_type="item",
               canonical_name="ключ"),
    ])
    db_session.add(Item(entity_id=str(item_id), current_owner_id=str(hero)))
    await db_session.flush()
    # The snapshot predates the transfer, as required by the production executor.
    await InitialWorldStateService(db_session).ensure_snapshot(cid)
    db_session.add_all([
        Turn(id=str(user_id), campaign_id=str(cid), role="user",
             content="Передаю ключ Ивару.", status="active"),
        Turn(id=str(assistant_id), campaign_id=str(cid), role="assistant",
             parent_turn_id=str(user_id), content="Ивар взял ключ.", status="active"),
        Turn(id=str(unrelated_id), campaign_id=str(cid), role="user",
             content="Смотрю в окно.", status="undone"),
    ])
    await db_session.flush()
    sequence = ActionSequence(
        campaign_id=str(cid), trigger_turn_id=str(user_id),
        status="applied", planned_steps=1, completed_steps=1,
    )
    db_session.add(sequence)
    await db_session.flush()
    db_session.add(ActionStep(
        sequence_id=sequence.id, step_index=0, action_type="inventory",
        intent="Передать ключ", resolution="auto_success",
        status="completed", item_id=str(item_id), item_operation="give",
        item_previous_owner_id=str(hero), item_result_owner_id=str(recipient),
    ))
    item = await db_session.get(Item, str(item_id))
    item.current_owner_id = str(recipient)
    await db_session.flush()

    replay = ActiveCanonReplayService(db_session)
    await replay.replay(cid)
    await db_session.refresh(item)
    assert item.current_owner_id == str(recipient)
    assert item.current_location_id is None
    # A second undo/replay is idempotent.
    await replay.replay(cid)
    await db_session.refresh(item)
    assert item.current_owner_id == str(recipient)

    # Undo of the *actual* owning step should restore the original custody.
    sequence.status = "undone"
    # Production undo flushes the compensated ActionSequence before replay.
    await db_session.flush()
    await replay.replay(cid)
    await db_session.refresh(item)
    assert item.current_owner_id == str(hero)


@pytest.mark.asyncio
async def test_replay_restores_structured_placement_detail(
    db_session: AsyncSession,
):
    cid, item_id, room_id, user_id = (uuid4() for _ in range(4))
    db_session.add(Campaign(id=str(cid), name="F-32 placement replay"))
    db_session.add_all([
        Entity(id=str(item_id), campaign_id=str(cid), entity_type="item",
               canonical_name="письмо"),
        Entity(id=str(room_id), campaign_id=str(cid), entity_type="location",
               canonical_name="Горница"),
    ])
    db_session.add(Item(entity_id=str(item_id), current_location_id=str(room_id),
                        placement_detail="на полке"))
    await db_session.flush()
    await InitialWorldStateService(db_session).ensure_snapshot(cid)
    db_session.add(Turn(id=str(user_id), campaign_id=str(cid), role="user",
                        content="Переставляю письмо к лампе.", status="active"))
    await db_session.flush()
    sequence = ActionSequence(campaign_id=str(cid), trigger_turn_id=str(user_id),
                              status="applied", planned_steps=1, completed_steps=1)
    db_session.add(sequence)
    await db_session.flush()
    db_session.add(ActionStep(
        sequence_id=sequence.id, step_index=0, action_type="inventory",
        intent="Переставить письмо", resolution="auto_success",
        status="completed", item_id=str(item_id), item_operation="reposition",
        item_previous_location_id=str(room_id),
        item_previous_placement_detail="на полке",
        item_result_location_id=str(room_id),
        item_result_placement_detail="между лампой и стеной",
    ))
    item = await db_session.get(Item, str(item_id))
    item.placement_detail = "между лампой и стеной"
    await db_session.flush()

    await ActiveCanonReplayService(db_session).replay(cid)
    await db_session.refresh(item)
    assert item.placement_detail == "между лампой и стеной"
    assert item.current_owner_id is None
    assert item.current_location_id == str(room_id)
