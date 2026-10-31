"""verify-r3 T6r: /turns/{id}/retry reuses the failed user turn's ID; its rolled-back action sequence
must give way, or the retry dies on UNIQUE action_sequences.trigger_turn_id."""
import pytest
from sqlalchemy import func, select

from app.db.action_sequence_table import ActionSequence
from app.services.action_sequence_executor import ActionSequenceExecutor
from tests.test_coin_merge_back import _item, _step
from tests.test_qa219_regressions import _world
from tests.test_qa223_regressions import ActionPlanCompiler, TurnCreate, TurnRepository


@pytest.mark.asyncio
async def test_a_rolled_back_sequence_does_not_block_the_same_turns_retry(db_session):
    cid, room, _, hero, _, _, scene = await _world(db_session)
    coin = await _item(db_session, cid, "серебряк", 1, owner=hero)
    contract, decision = _step(coin, "place")
    user = await TurnRepository(db_session).create(cid, TurnCreate(role="user", content="Монета.", scene_id=scene))
    executor = ActionSequenceExecutor(db_session)
    plan = await ActionPlanCompiler(db_session).compile(cid, contract, decision, player_input="Монета.")
    first = await executor.execute(cid, scene, user.id, plan.action_sequence)
    assert await executor.rollback_prepared(first.sequence_id)
    await db_session.flush()
    plan = await ActionPlanCompiler(db_session).compile(cid, contract, decision, player_input="Монета.")
    second = await executor.execute(cid, scene, user.id, plan.action_sequence)
    assert second.status == "prepared" and second.steps[0].status == "completed"
    count = (await db_session.execute(select(func.count()).select_from(ActionSequence).where(
        ActionSequence.trigger_turn_id == str(user.id)))).scalar_one()
    assert count == 1
