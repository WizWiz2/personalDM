"""Real SQLite single-writer contention and restart recovery of stuck generation work."""
import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.db.generation_lifecycle_table import GenerationLifecycle
from app.db.narration_validation_table import NarrationValidationRun
from app.db.repositories.generation_lifecycle_repo import GenerationLifecycleRepository
from app.db.repositories.job_repo import GenerationRunRepository, PostTurnJobRepository
from app.db.repositories.turn_repo import TurnRepository
from app.db.tables import GenerationRun, PostTurnJob, Turn
from app.models.campaign import CampaignCreate
from app.models.jobs import GenerationPhase
from app.models.turn import TurnCreate
from app.services.campaign_service import CampaignService
from app.services.generation_startup_recovery import recover_interrupted_generations


async def _campaign(file_db):
    async with file_db() as session:
        cid = (await CampaignService(session).create_campaign(CampaignCreate(name="contention"))).id
        await session.commit()
    return cid


@pytest.mark.asyncio
async def test_turn_commit_waits_for_a_concurrent_writer_instead_of_failing(file_db):
    cid = await _campaign(file_db)
    background = file_db.second_engine()
    holding = asyncio.Event()

    async def background_writer():
        async with background() as session:
            await TurnRepository(session).create(cid, TurnCreate(role="user", content="фон"))
            await session.flush()  # takes the write lock
            holding.set()
            await asyncio.sleep(1.5)
            await session.commit()

    async def foreground_turn():
        await holding.wait()
        async with file_db() as session:
            await TurnRepository(session).create(cid, TurnCreate(role="user", content="ход"))
            await session.commit()

    await asyncio.gather(background_writer(), foreground_turn())
    async with file_db() as other:
        contents = (await other.execute(select(Turn.content).where(Turn.campaign_id == str(cid)))).scalars().all()
    assert sorted(contents) == ["фон", "ход"]


@pytest.mark.asyncio
async def test_reader_is_not_blocked_by_an_open_write_transaction(file_db):
    cid = await _campaign(file_db)
    async with file_db.second_engine()() as writer:
        await TurnRepository(writer).create(cid, TurnCreate(role="user", content="пишу"))
        await writer.flush()
        async with file_db() as reader:
            rows = await asyncio.wait_for(reader.execute(select(Turn).where(Turn.campaign_id == str(cid))), 2)
            assert rows.scalars().all() == []
        await writer.commit()


async def _stuck_run(file_db, cid, phase, *, published=False, audit=False):
    async with file_db() as session:
        turns = TurnRepository(session)
        user = await turns.create(cid, TurnCreate(role="user", content="ход"))
        run = await GenerationRunRepository(session).start_or_resume(cid, user.id)
        await GenerationLifecycleRepository(session).start_attempt(run.id)
        if phase is not GenerationPhase.RECEIVED:
            await GenerationLifecycleRepository(session).set_phase(run.id, phase)
        if published:
            assistant = await turns.create(cid, TurnCreate(role="assistant", content="ответ", parent_turn_id=user.id))
            (await session.get(GenerationRun, str(run.id))).assistant_turn_id = str(assistant.id)
        if audit:
            session.add(NarrationValidationRun(campaign_id=str(cid), trigger_turn_id=str(user.id), draft_text="d"))
        await session.commit()
        return user.id, run.id


async def _states(file_db, user_id, run_id):
    async with file_db() as other:
        run = await other.get(GenerationRun, str(run_id))
        user = await other.get(Turn, str(user_id))
        audits = (await other.execute(select(NarrationValidationRun.status).where(
            NarrationValidationRun.trigger_turn_id == str(user_id)))).scalars().all()
        return run.status, user.status, audits


@pytest.mark.asyncio
async def test_restart_fails_an_unprepared_run_and_its_validating_audit(file_db):
    cid = await _campaign(file_db)
    user_id, run_id = await _stuck_run(file_db, cid, GenerationPhase.PLANNED, audit=True)
    async with file_db() as session:
        assert await recover_interrupted_generations(session) == 1
    assert await _states(file_db, user_id, run_id) == ("failed", "failed", ["failed"])


@pytest.mark.asyncio
async def test_restart_completes_a_run_whose_answer_was_already_published(file_db):
    cid = await _campaign(file_db)
    user_id, run_id = await _stuck_run(file_db, cid, GenerationPhase.PLANNED,
                                       published=True)
    async with file_db() as session:
        assert await recover_interrupted_generations(session) == 1
    assert await _states(file_db, user_id, run_id) == ("completed", "active", [])


@pytest.mark.asyncio
async def test_restart_with_nothing_stuck_changes_nothing(file_db):
    cid = await _campaign(file_db)
    async with file_db() as session:
        assert await recover_interrupted_generations(session) == 0


@pytest.mark.asyncio
async def test_restart_after_a_prepared_crash_compensates_instead_of_refusing_to_start(file_db):
    cid = await _campaign(file_db)
    user_id, run_id = await _stuck_run(file_db, cid, GenerationPhase.PREPARED, audit=True)
    async with file_db() as session:
        await recover_interrupted_generations(session)
    assert await _states(file_db, user_id, run_id) == ("failed", "failed", ["failed"])
    async with file_db() as other:
        assert (await other.get(GenerationLifecycle, str(run_id))).phase == GenerationPhase.COMPENSATED.value


@pytest.mark.asyncio
async def test_restart_recovers_every_stuck_item_and_is_idempotent(file_db):
    cid = await _campaign(file_db)
    planned = await _stuck_run(file_db, cid, GenerationPhase.PLANNED, audit=True)
    prepared = await _stuck_run(file_db, cid, GenerationPhase.PREPARED, audit=True)
    narrated = await _stuck_run(file_db, cid, GenerationPhase.NARRATED)
    async with file_db() as session:
        user = await TurnRepository(session).create(cid, TurnCreate(role="user", content="ход"))
        assistant = await TurnRepository(session).create(
            cid, TurnCreate(role="assistant", content="ответ", parent_turn_id=user.id))
        session.add(PostTurnJob(campaign_id=str(cid), assistant_turn_id=str(assistant.id), job_type="memory",
                                status="running", locked_at=datetime.utcnow() - timedelta(seconds=1)))
        await session.commit()
    async with file_db() as session:
        assert await recover_interrupted_generations(session) == 3
    async with file_db() as session:
        assert await recover_interrupted_generations(session) == 0
    assert await _states(file_db, *planned) == ("failed", "failed", ["failed"])
    assert await _states(file_db, *prepared) == ("failed", "failed", ["failed"])
    assert (await _states(file_db, *narrated))[:2] == ("failed", "failed")
    async with file_db() as other:
        phases = [(await other.get(GenerationLifecycle, str(run))).phase for _, run in (prepared, narrated)]
        jobs = (await other.execute(select(PostTurnJob.status))).scalars().all()
    assert phases == [GenerationPhase.COMPENSATED.value] * 2
    assert jobs == ["pending"]


@pytest.mark.asyncio
async def test_one_unrecoverable_run_does_not_stop_recovery_of_others(file_db, monkeypatch):
    from app.services import generation_startup_recovery as recovery

    cid = await _campaign(file_db)
    bad = await _stuck_run(file_db, cid, GenerationPhase.PREPARED)
    good = await _stuck_run(file_db, cid, GenerationPhase.PLANNED)

    async def broken(*_args):
        raise RuntimeError("corrupt prepared record")

    monkeypatch.setattr(recovery, "compensate_prepared_turn", broken)
    async with file_db() as session:
        assert await recover_interrupted_generations(session) == 1
    assert (await _states(file_db, *good))[:2] == ("failed", "failed")
    assert (await _states(file_db, *bad))[:2] == ("running", "active")


@pytest.mark.asyncio
async def test_stale_running_post_turn_job_is_requeued_and_fresh_one_is_kept(file_db):
    cid = await _campaign(file_db)
    async with file_db() as session:
        user = await TurnRepository(session).create(cid, TurnCreate(role="user", content="ход"))
        assistant = await TurnRepository(session).create(cid, TurnCreate(role="assistant", content="ответ",
                                                                         parent_turn_id=user.id))
        now = datetime.utcnow()
        for job_type, locked in (("stale", now - timedelta(minutes=10)), ("fresh", now)):
            session.add(PostTurnJob(campaign_id=str(cid), assistant_turn_id=str(assistant.id), job_type=job_type,
                                    status="running", locked_at=locked))
        await session.commit()
    async with file_db() as session:
        assert await PostTurnJobRepository(session).recover_stale() == 1
        await session.commit()
    async with file_db() as other:
        rows = dict((await other.execute(select(PostTurnJob.job_type, PostTurnJob.status))).all())
    assert rows == {"stale": "pending", "fresh": "running"}
