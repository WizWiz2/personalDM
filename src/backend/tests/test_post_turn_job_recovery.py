from datetime import datetime, timedelta
from uuid import uuid4
from uuid import UUID
import asyncio
from unittest.mock import AsyncMock

import pytest

from app.db.repositories.job_repo import PostTurnJobRepository
from app.db.tables import Campaign, PostTurnJob, Turn
from app.services.post_turn_processor import PostTurnProcessor


@pytest.mark.asyncio
async def test_recover_stale_reclaims_running_job_without_lock_timestamp(db_session):
    campaign_id = str(uuid4())
    turn_id = str(uuid4())
    db_session.add(Campaign(id=campaign_id, name="Recovery"))
    db_session.add(
        Turn(
            id=turn_id,
            campaign_id=campaign_id,
            role="assistant",
            content="ok",
            status="active",
        )
    )
    job = PostTurnJob(
        campaign_id=campaign_id,
        assistant_turn_id=turn_id,
        job_type="memory_scribe",
        status="running",
        updated_at=datetime.utcnow() - timedelta(hours=1),
        locked_at=None,
    )
    db_session.add(job)
    await db_session.flush()

    recovered = await PostTurnJobRepository(db_session).recover_stale()
    await db_session.flush()

    assert recovered == 1
    await db_session.refresh(job)
    assert job.status == "pending"
    assert job.locked_at is None


@pytest.mark.asyncio
async def test_cancelled_memory_job_is_immediately_retryable(db_session):
    campaign_id, turn_id = str(uuid4()), str(uuid4())
    db_session.add(Campaign(id=campaign_id, name="Cancelled memory"))
    db_session.add(
        Turn(
            id=turn_id,
            campaign_id=campaign_id,
            role="assistant",
            content="Saved reply",
            status="active",
        )
    )
    job = PostTurnJob(
        campaign_id=campaign_id,
        assistant_turn_id=turn_id,
        job_type="memory_scribe",
        status="pending",
    )
    db_session.add(job)
    await db_session.commit()
    job_id = UUID(job.id)
    processor = PostTurnProcessor(db_session)
    processor._turns.get_by_id = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await processor.process_job(job_id)
    await db_session.refresh(job)
    assert job.status == "pending"
    assert job.locked_at is None


@pytest.mark.asyncio
async def test_post_turn_watchdog_marks_stalled_job_failed(db_session, monkeypatch):
    """An unresponsive model must not strand the durable row in 'running'."""
    from app.config import settings

    campaign_id, turn_id = str(uuid4()), str(uuid4())
    db_session.add(Campaign(id=campaign_id, name="Hung background memory"))
    db_session.add(Turn(
        id=turn_id, campaign_id=campaign_id, role="assistant",
        content="Published turn", status="active",
    ))
    job = PostTurnJob(
        campaign_id=campaign_id, assistant_turn_id=turn_id,
        job_type="memory_scribe", status="running", attempts=1,
    )
    db_session.add(job)
    await db_session.commit()
    job_id = UUID(job.id)
    processor = PostTurnProcessor(db_session)
    monkeypatch.setattr(settings, "POST_TURN_JOB_TIMEOUT_SECONDS", 0.02)

    async def stalled(_id, *, already_claimed=False):
        assert already_claimed is True
        await asyncio.sleep(60)

    monkeypatch.setattr(processor, "process_job", stalled)
    with pytest.raises(RuntimeError, match="timed out"):
        await processor.process_job_bounded(job_id, already_claimed=True)

    row = await db_session.get(PostTurnJob, str(job_id))
    await db_session.refresh(row)
    assert row.status == "failed"
    assert "timed out" in row.error
    assert row.locked_at is None
    assert row.attempts == 1

    retried = await PostTurnJobRepository(db_session).retry(job_id)
    assert retried.status == "pending"


@pytest.mark.asyncio
async def test_recover_stale_does_not_steal_recent_null_lock(db_session):
    campaign_id, turn_id = str(uuid4()), str(uuid4())
    db_session.add(Campaign(id=campaign_id, name="Live null lease"))
    db_session.add(Turn(id=turn_id, campaign_id=campaign_id, role="assistant",
                        content="ok", status="active"))
    job = PostTurnJob(campaign_id=campaign_id, assistant_turn_id=turn_id,
                      job_type="memory_scribe", status="running", locked_at=None,
                      updated_at=datetime.utcnow())
    db_session.add(job)
    await db_session.commit()
    assert await PostTurnJobRepository(db_session).recover_stale(60) == 0
    await db_session.refresh(job)
    assert job.status == "running"


@pytest.mark.asyncio
async def test_failed_job_auto_retry_is_delayed_and_bounded(db_session):
    campaign_id, turn_id = str(uuid4()), str(uuid4())
    db_session.add(Campaign(id=campaign_id, name="Retry budget"))
    db_session.add(Turn(id=turn_id, campaign_id=campaign_id, role="assistant",
                        content="ok", status="active"))
    job = PostTurnJob(campaign_id=campaign_id, assistant_turn_id=turn_id,
                      job_type="memory_scribe", status="failed", attempts=1,
                      error="database is locked", updated_at=datetime.utcnow())
    db_session.add(job)
    await db_session.commit()
    repo = PostTurnJobRepository(db_session)
    assert await repo.requeue_failed(base_delay_seconds=10) == 0
    job.updated_at = datetime.utcnow() - timedelta(seconds=30)
    await db_session.commit()
    assert await repo.requeue_failed(base_delay_seconds=10) == 1
    await db_session.refresh(job)
    assert job.status == "pending"
    job.status = "failed"
    job.attempts = repo.MAX_AUTO_ATTEMPTS
    job.updated_at = datetime.utcnow() - timedelta(hours=1)
    await db_session.commit()
    assert await repo.requeue_failed(base_delay_seconds=1) == 0
    await db_session.refresh(job)
    assert job.status == "failed"


@pytest.mark.asyncio
async def test_periodic_sweep_recovers_durable_jobs_without_worker_restart(tmp_path, monkeypatch):
    """Regression for the real P0-C: status=running (including NULL lease)
    and failed jobs must recover while the backend process remains alive.
    Use *file-backed* SQLite, not the shared in-memory test transaction.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from app.db.engine import Base
    from app.services import post_turn_processor as post_turn_module

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'recovery.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        campaign_id, turn_id = str(uuid4()), str(uuid4())
        old = datetime.utcnow() - timedelta(hours=2)
        async with factory() as session:
            session.add(Campaign(id=campaign_id, name="Background recovery"))
            session.add(Turn(id=turn_id, campaign_id=campaign_id,
                             role="assistant", content="Published", status="active"))
            stuck = PostTurnJob(campaign_id=campaign_id, assistant_turn_id=turn_id,
                                job_type="thesis_curator", status="running",
                                attempts=1, locked_at=None, updated_at=old)
            failed = PostTurnJob(campaign_id=campaign_id, assistant_turn_id=turn_id,
                                 job_type="memory_scribe", status="failed",
                                 attempts=1, error="database is locked", updated_at=old)
            session.add_all([stuck, failed])
            await session.commit()
            stuck_id, failed_id = stuck.id, failed.id

        monkeypatch.setattr(post_turn_module, "AsyncSessionLocal", factory)
        worker = post_turn_module.PostTurnWorker()
        sweep = asyncio.create_task(worker._recover_loop())
        try:
            for _ in range(50):
                async with factory() as session:
                    one = await session.get(PostTurnJob, stuck_id)
                    two = await session.get(PostTurnJob, failed_id)
                    if one.status == two.status == "pending":
                        break
                await asyncio.sleep(0.02)
            else:
                pytest.fail("Periodic DB sweep did not reclaim stale/failed jobs")
        finally:
            worker.stop()
            await asyncio.wait_for(sweep, timeout=2.0)

        async with factory() as session:
            one = await session.get(PostTurnJob, stuck_id)
            two = await session.get(PostTurnJob, failed_id)
            assert (one.status, one.locked_at) == ("pending", None)
            assert (two.status, two.attempts) == ("pending", 1)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_idle_recovery_sweep_does_not_acquire_sqlite_write_lock(db_session):
    """QA-210 F-29: no unconditional UPDATE while the foreground publishes."""
    from sqlalchemy import event as sqlalchemy_event

    statements = []
    engine = db_session.bind.sync_engine

    def record_statement(_conn, _cursor, statement, _parameters, _context, _many):
        if "post_turn_jobs" in statement.lower():
            statements.append(statement.lower().lstrip())

    sqlalchemy_event.listen(engine, "before_cursor_execute", record_statement)
    try:
        assert await PostTurnJobRepository(db_session).recover_stale() == 0
    finally:
        sqlalchemy_event.remove(engine, "before_cursor_execute", record_statement)
    assert any(statement.startswith("select") for statement in statements)
    assert not any(statement.startswith("update") for statement in statements)
