"""QA-304-7: a created document has no pre-turn custody to resurrect on undo."""
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from app.db.repositories.turn_repo import TurnRepository
from app.db.tables import Entity, Item
from app.db.truth_engine_table import FluentAssertion, TruthEventRecord
from app.models.player_intent import (
    ActionOutcomeDecision,
    PlayerActionIntent,
    PlayerIntentContract,
    TurnOutcomeDecision,
)
from app.models.turn import TurnCreate
from app.models.turn_authority import PlannedItemIntroduction
from app.services.action_sequence_executor import ActionSequenceExecutor
from app.services.turn_undo_service import TurnUndoService
from tests.test_qa219_regressions import _world
from tests.test_qa223_regressions import _execute


@pytest.mark.asyncio
async def test_undo_created_then_stored_document_retires_its_legacy_baseline(db_session):
    cid, _, _, _, _, old_item, scene = await _world(db_session)
    await db_session.commit()
    await db_session.execute(text("PRAGMA foreign_keys=ON"))
    contract = PlayerIntentContract(summary="Написать лист и убрать в карман.", actions=[
        PlayerActionIntent(action_type="interaction", intent="Написать лист.", expected_effect="item_creation"),
        PlayerActionIntent(action_type="inventory", intent="Убрать лист.", produced_item_action_index=0,
                           produced_item_ordinal=0, inventory_operation="store"),
    ])
    decision = TurnOutcomeDecision(action_outcomes=[ActionOutcomeDecision(action_index=i,
        resolution="auto_success", observable_outcome="Готово.") for i in range(2)], item_introductions=[
        PlannedItemIntroduction(canonical_name="Новый лист", after_action_index=0,
                                evidence="Написан лист.", owned_by_player=True)])
    execution = await _execute(db_session, cid, scene, contract, decision)
    await ActionSequenceExecutor(db_session).mark_applied(execution.sequence_id)
    from app.db.action_sequence_table import ActionSequence
    from app.services.truth_engine_receipts import StructuredReceiptEventCompiler
    sequence = await db_session.get(ActionSequence, str(execution.sequence_id))
    await StructuredReceiptEventCompiler(db_session)._compile_sequence(SimpleNamespace(
        trigger_turn_id=sequence.trigger_turn_id, campaign_id=str(cid)))
    item_id = str(execution.steps[1].item_id)
    # Old saves contain unscoped legacy baselines, including QA-304's failing document.
    records = (await db_session.execute(select(TruthEventRecord).where(
        TruthEventRecord.event_key == f"baseline:item_position:{item_id}"))).scalars().all()
    assert len(records) == 1
    records[0].source_turn_id = None
    baseline_id = records[0].event_id
    await TurnRepository(db_session).create(cid, TurnCreate(role="assistant", content="Лист убран.",
        scene_id=scene, parent_turn_id=sequence.trigger_turn_id))
    await db_session.commit()

    assert await TurnUndoService(db_session).undo_last_pair(cid)
    await db_session.commit()
    await db_session.execute(text("PRAGMA foreign_keys=OFF"))
    db_session.expire_all()
    assert await db_session.get(Entity, item_id) is None
    assert await db_session.get(Item, str(old_item)) is not None
    assert not (await db_session.execute(select(FluentAssertion).where(
        FluentAssertion.subject_entity_id == item_id))).scalars().all()
    assert (await db_session.get(TruthEventRecord, baseline_id)).status == "reverted"
