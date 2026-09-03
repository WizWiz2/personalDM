from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.action_sequence_table import ActionSequence, ActionStep
from app.db.tables import Campaign, Entity, Item, Turn
from app.models.proposed_change import ChangeType
from app.services.action_sequence_executor import ActionSequenceExecutor
from app.services.canon_applier import CanonApplier


@pytest.mark.asyncio
async def test_scribe_item_transfer_cannot_overwrite_same_turn_structured_receipt(
    db_session: AsyncSession,
):
    campaign_id = uuid4()
    hero_id = uuid4()
    npc_id = uuid4()
    item_id = uuid4()
    room_id = uuid4()
    user_turn_id = uuid4()
    assistant_turn_id = uuid4()

    db_session.add(Campaign(id=str(campaign_id), name="Structured item authority"))
    db_session.add_all(
        [
            Entity(
                id=str(hero_id),
                campaign_id=str(campaign_id),
                entity_type="character",
                canonical_name="Кай",
            ),
            Entity(
                id=str(npc_id),
                campaign_id=str(campaign_id),
                entity_type="character",
                canonical_name="Мартин Вэнс",
            ),
            Entity(
                id=str(item_id),
                campaign_id=str(campaign_id),
                entity_type="item",
                canonical_name="латунный ключ",
            ),
            Entity(
                id=str(room_id),
                campaign_id=str(campaign_id),
                entity_type="location",
                canonical_name="Комната Кая",
            ),
        ]
    )
    db_session.add(Item(entity_id=str(item_id), current_owner_id=str(npc_id)))
    db_session.add_all(
        [
            Turn(
                id=str(user_turn_id),
                campaign_id=str(campaign_id),
                role="user",
                content="Передаю латунный ключ Мартину Вэнсу.",
            ),
            Turn(
                id=str(assistant_turn_id),
                campaign_id=str(campaign_id),
                role="assistant",
                content="Мартин принимает ключ.",
                parent_turn_id=str(user_turn_id),
            ),
        ]
    )
    sequence = ActionSequence(
        campaign_id=str(campaign_id),
        trigger_turn_id=str(user_turn_id),
        status="applied",
        planned_steps=1,
        completed_steps=1,
    )
    db_session.add(sequence)
    await db_session.flush()
    db_session.add(
        ActionStep(
            sequence_id=sequence.id,
            step_index=0,
            action_type="inventory",
            intent="Передать ключ Мартину",
            resolution="auto_success",
            status="completed",
            item_id=str(item_id),
            item_name="латунный ключ",
            item_operation="give",
            item_previous_owner_id=str(hero_id),
            item_result_owner_id=str(npc_id),
        )
    )
    await db_session.flush()

    await CanonApplier(db_session).apply(
        campaign_id,
        ChangeType.ITEM_TRANSFER,
        {
            "item_id": str(item_id),
            # This is the exact dangerous shape seen from the live Scribe: prose says
            # "gave", but the structured owner field is missing and only scene location remains.
            "owner_id": None,
            "location_id": str(room_id),
            "description": "Кай передал латунный ключ Мартину Вэнсу.",
        },
        assistant_turn_id,
    )
    await db_session.flush()

    item = await db_session.get(Item, str(item_id))
    assert item is not None
    assert item.current_owner_id == str(npc_id)
    assert item.current_location_id is None


@pytest.mark.asyncio
async def test_inventory_execution_refusal_uses_non_transition_error_boundary(
    db_session: AsyncSession,
):
    executor = ActionSequenceExecutor(db_session)
    step = SimpleNamespace(item_id=None, inventory_operation=None)

    with pytest.raises(RuntimeError, match="Inventory step is missing its typed item operation") as exc:
        await executor._apply_inventory_step(
            uuid4(),
            None,
            uuid4(),
            step,
            SimpleNamespace(),
        )

    # TurnSaga intentionally treats ValueError as a recoverable transition fallback. A structured
    # inventory refusal must bypass that mask so the durable generation error keeps the real cause.
    assert not isinstance(exc.value, ValueError)
