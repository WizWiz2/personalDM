from datetime import datetime, timedelta
from uuid import UUID

import pytest

from app.db.repositories.turn_repo import TurnRepository
from app.db.tables import Campaign, PostTurnJob, Scene, Turn
from app.services.post_turn_processor import PostTurnProcessor


async def create_turn_fixture(db_session):
    campaign = Campaign(name="Post-turn scheduling")
    db_session.add(campaign)
    await db_session.flush()
    scene = Scene(campaign_id=campaign.id, title="Stable scene")
    db_session.add(scene)
    await db_session.flush()
    user = Turn(
        campaign_id=campaign.id,
        scene_id=scene.id,
        role="user",
        content="Проверяю дверь",
    )
    db_session.add(user)
    await db_session.flush()
    assistant = Turn(
        campaign_id=campaign.id,
        scene_id=scene.id,
        role="assistant",
        content="Дверь поддалась",
        parent_turn_id=user.id,
    )
    db_session.add(assistant)
    await db_session.flush()
    return campaign, assistant


@pytest.mark.asyncio
async def test_assistant_turn_number_is_stable_for_older_jobs(db_session):
    campaign = Campaign(name="Turn ordinal")
    db_session.add(campaign)
    await db_session.flush()
    scene = Scene(campaign_id=campaign.id, title="Ordinal scene")
    db_session.add(scene)
    await db_session.flush()
    start = datetime.utcnow()
    turns = []
    for index in range(3):
        turn = Turn(
            campaign_id=campaign.id,
            scene_id=scene.id,
            role="assistant",
            content=f"Ответ {index + 1}",
            created_at=start + timedelta(seconds=index),
        )
        db_session.add(turn)
        turns.append(turn)
    await db_session.flush()

    repo = TurnRepository(db_session)
    numbers = [
        await repo.assistant_turn_number_in_scene(UUID(turn.id))
        for turn in turns
    ]
    assert numbers == [1, 2, 3]


@pytest.mark.asyncio
async def test_running_job_is_not_processed_twice(db_session, monkeypatch):
    campaign, assistant = await create_turn_fixture(db_session)
    job = PostTurnJob(
        campaign_id=campaign.id,
        assistant_turn_id=assistant.id,
        job_type="thesis_curator",
        status="running",
        attempts=1,
    )
    db_session.add(job)
    await db_session.commit()
    calls = 0

    async def unexpected_call(*args, **kwargs):
        nonlocal calls
        calls += 1

    monkeypatch.setattr(
        "app.services.post_turn_processor.ThesisCurator.curate_after_turn",
        unexpected_call,
    )
    await PostTurnProcessor(db_session).process_job(UUID(job.id))
    await db_session.refresh(job)

    assert calls == 0
    assert job.status == "running"
    assert job.attempts == 1


@pytest.mark.asyncio
async def test_claimed_worker_job_is_processed(db_session, monkeypatch):
    campaign, assistant = await create_turn_fixture(db_session)
    job = PostTurnJob(
        campaign_id=campaign.id,
        assistant_turn_id=assistant.id,
        job_type="thesis_curator",
        status="running",
        attempts=1,
    )
    db_session.add(job)
    await db_session.commit()
    calls = 0

    async def curated(*args, **kwargs):
        nonlocal calls
        calls += 1

    monkeypatch.setattr(
        "app.services.post_turn_processor.ThesisCurator.curate_after_turn",
        curated,
    )
    await PostTurnProcessor(db_session).process_job(
        UUID(job.id),
        already_claimed=True,
    )
    await db_session.refresh(job)

    assert calls == 1
    assert job.status == "completed"
    assert job.attempts == 1
