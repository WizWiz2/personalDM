from datetime import datetime, timedelta
from uuid import UUID

import pytest

from app.config import settings
from app.db.repositories.job_repo import PostTurnJobRepository
from app.db.repositories.turn_repo import TurnRepository
from app.db.tables import Campaign, PostTurnJob, Scene, Turn
from app.services.post_turn_processor import PostTurnProcessor
from app.services.truth_engine_shadow import SemanticResidualShadowService


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


@pytest.mark.asyncio
async def test_te2_shadow_is_enqueued_as_durable_job_only_when_enabled(db_session, monkeypatch):
    campaign, assistant = await create_turn_fixture(db_session)
    processor = PostTurnProcessor(db_session)

    monkeypatch.setattr(settings, "TE2_SEMANTIC_SHADOW_ENABLED", False)
    await processor.enqueue(UUID(campaign.id), UUID(assistant.id))
    jobs = await PostTurnJobRepository(db_session).list_for_turn(UUID(assistant.id))
    assert {job.job_type for job in jobs} == {"memory_scribe", "thesis_curator"}

    monkeypatch.setattr(settings, "TE2_SEMANTIC_SHADOW_ENABLED", True)
    await processor.enqueue(UUID(campaign.id), UUID(assistant.id))
    jobs = await PostTurnJobRepository(db_session).list_for_turn(UUID(assistant.id))
    assert {job.job_type for job in jobs} == {
        "memory_scribe",
        "thesis_curator",
        "te2_semantic_shadow",
    }


@pytest.mark.asyncio
async def test_te2_shadow_job_stays_nonterminal_until_capture_finishes(db_session, monkeypatch):
    campaign, assistant = await create_turn_fixture(db_session)
    monkeypatch.setattr(settings, "TE2_SEMANTIC_SHADOW_ENABLED", True)
    processor = PostTurnProcessor(db_session)
    await processor.enqueue(UUID(campaign.id), UUID(assistant.id))
    await db_session.commit()

    jobs = await PostTurnJobRepository(db_session).list_for_turn(UUID(assistant.id))
    shadow = next(job for job in jobs if job.job_type == "te2_semantic_shadow")
    observed_statuses: list[str] = []

    async def capture(self, assistant_turn_id):
        row = await db_session.get(PostTurnJob, str(shadow.id))
        await db_session.refresh(row)
        observed_statuses.append(row.status)
        assert assistant_turn_id == UUID(assistant.id)
        return True

    monkeypatch.setattr(SemanticResidualShadowService, "capture", capture)
    await processor.process_job(shadow.id)

    row = await db_session.get(PostTurnJob, str(shadow.id))
    await db_session.refresh(row)
    assert observed_statuses == ["running"]
    assert row.status == "completed"
    assert row.attempts == 1


@pytest.mark.asyncio
async def test_te2_shadow_failure_is_terminal_and_retryable(db_session, monkeypatch):
    campaign, assistant = await create_turn_fixture(db_session)
    monkeypatch.setattr(settings, "TE2_SEMANTIC_SHADOW_ENABLED", True)
    processor = PostTurnProcessor(db_session)
    await processor.enqueue(UUID(campaign.id), UUID(assistant.id))
    await db_session.commit()

    jobs = await PostTurnJobRepository(db_session).list_for_turn(UUID(assistant.id))
    shadow = next(job for job in jobs if job.job_type == "te2_semantic_shadow")

    async def fail_capture(*args, **kwargs):
        raise RuntimeError("shadow model unavailable")

    monkeypatch.setattr(SemanticResidualShadowService, "capture", fail_capture)
    with pytest.raises(RuntimeError, match="shadow model unavailable"):
        await processor.process_job(shadow.id)

    row = await db_session.get(PostTurnJob, str(shadow.id))
    await db_session.refresh(row)
    assert row.status == "failed"
    assert row.attempts == 1

    retried = await PostTurnJobRepository(db_session).retry(shadow.id)
    assert retried is not None
    assert retried.status == "pending"
