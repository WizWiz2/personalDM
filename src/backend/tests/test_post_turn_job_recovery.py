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
