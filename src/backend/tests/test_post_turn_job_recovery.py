from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from app.db.repositories.job_repo import PostTurnJobRepository
from app.db.tables import Campaign, PostTurnJob, Turn


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
