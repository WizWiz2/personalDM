"""QA-215 F-42 and N-13 recovery regressions."""
from uuid import UUID, uuid4

import pytest

from app.db.startup_migrations import classify_unversioned_schema
from app.db.tables import Campaign, GenerationRun, Turn
from app.db.generation_lifecycle_table import GenerationLifecycle
from app.services.generation_startup_recovery import recover_interrupted_generations
from app.db.repositories.job_repo import PostTurnJobRepository
from app.db.tables import PostTurnJob


def test_f42_known_legacy_with_corrupted_campaigns_is_not_stamped():
    tables = {"campaigns", "items", "action_steps", "memory_witnesses", "action_sequences"}
    steps = {"item_id", "item_previous_owner_id"}
    assert classify_unversioned_schema(
        tables, {"entity_id"}, steps, {"id", "nazvanie"}
    ) == "unknown"
    assert classify_unversioned_schema(
        tables, {"entity_id"}, steps, {"id", "name"}
    ) == "f3a4b5c6d7e8"


@pytest.mark.asyncio
async def test_n13_startup_marks_received_orphan_as_retryable(db_session):
    campaign_id, turn_id = uuid4(), uuid4()
    db_session.add(Campaign(id=str(campaign_id), name="N-13"))
    db_session.add(Turn(id=str(turn_id), campaign_id=str(campaign_id),
                        role="user", content="Иду в архив", status="active"))
    run = GenerationRun(campaign_id=str(campaign_id), user_turn_id=str(turn_id),
                        status="running")
    db_session.add(run)
    await db_session.flush()
    db_session.add(GenerationLifecycle(generation_run_id=run.id, phase="received"))
    await db_session.commit()
    assert await recover_interrupted_generations(db_session) == 1
    assert (await db_session.get(Turn, str(turn_id))).status == "failed"
    assert (await db_session.get(GenerationRun, run.id)).status == "failed"
    assert await recover_interrupted_generations(db_session) == 0


@pytest.mark.asyncio
async def test_n13_does_not_auto_recover_prepared_world(db_session):
    campaign_id, turn_id = uuid4(), uuid4()
    db_session.add(Campaign(id=str(campaign_id), name="Unsafe saga"))
    db_session.add(Turn(id=str(turn_id), campaign_id=str(campaign_id),
                        role="user", content="Перемещаюсь", status="active"))
    run = GenerationRun(campaign_id=str(campaign_id), user_turn_id=str(turn_id),
                        status="running")
    db_session.add(run)
    await db_session.flush()
    db_session.add(GenerationLifecycle(generation_run_id=run.id, phase="prepared"))
    await db_session.commit()
    with pytest.raises(RuntimeError, match="Unsafe interrupted generation"):
        await recover_interrupted_generations(db_session)
    assert (await db_session.get(GenerationRun, run.id)).status == "running"


@pytest.mark.asyncio
async def test_manual_retry_does_not_reclaim_running_memory_job(db_session):
    cid, tid = uuid4(), uuid4()
    db_session.add(Campaign(id=str(cid), name="Job retry safety"))
    db_session.add(Turn(id=str(tid), campaign_id=str(cid),
                        role="assistant", content="ok", status="active"))
    row = PostTurnJob(campaign_id=str(cid), assistant_turn_id=str(tid),
                      job_type="memory_scribe", status="running")
    db_session.add(row)
    await db_session.commit()
    result = await PostTurnJobRepository(db_session).retry(UUID(row.id))
    assert result.status == "running"
    assert (await db_session.get(PostTurnJob, row.id)).status == "running"
