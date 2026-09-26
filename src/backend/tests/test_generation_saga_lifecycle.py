import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.generation_lifecycle_repo import GenerationLifecycleRepository
from app.db.repositories.job_repo import GenerationRunRepository
from app.db.repositories.turn_repo import TurnRepository
from app.models.campaign import CampaignCreate
from app.models.jobs import GenerationPhase
from app.models.turn import TurnCreate
from app.services.campaign_service import CampaignService


@pytest.mark.asyncio
async def test_generation_phase_is_separate_from_run_status_and_recoverable(
    db_session: AsyncSession,
):
    campaign = await CampaignService(db_session).create_campaign(
        CampaignCreate(name="Saga lifecycle")
    )
    user_turn = await TurnRepository(db_session).create(
        campaign.id,
        TurnCreate(role="user", content="Осматриваюсь."),
    )
    runs = GenerationRunRepository(db_session)
    lifecycle = GenerationLifecycleRepository(db_session)
    run = await runs.start_or_resume(campaign.id, user_turn.id)
    started = await lifecycle.start_attempt(run.id)

    assert started.phase == GenerationPhase.RECEIVED
    assert run.status == "running"

    await lifecycle.set_phase(run.id, GenerationPhase.PLANNED)
    prepared = await lifecycle.set_phase(run.id, GenerationPhase.PREPARED)
    await runs.set_status(run.id, "failed", error="simulated crash")
    await db_session.commit()

    assert prepared.prepared_at is not None
    incomplete = await lifecycle.list_incomplete(campaign.id)
    assert [item.generation_run_id for item in incomplete] == [run.id]

    compensated = await lifecycle.set_phase(run.id, GenerationPhase.COMPENSATED)
    await db_session.commit()
    assert compensated.compensated_at is not None
    assert await lifecycle.list_incomplete(campaign.id) == []


@pytest.mark.asyncio
async def test_resumed_generation_starts_new_lifecycle_attempt(
    db_session: AsyncSession,
):
    campaign = await CampaignService(db_session).create_campaign(
        CampaignCreate(name="Saga retry")
    )
    user_turn = await TurnRepository(db_session).create(
        campaign.id,
        TurnCreate(role="user", content="Пробую снова."),
    )
    runs = GenerationRunRepository(db_session)
    lifecycle = GenerationLifecycleRepository(db_session)
    run = await runs.start_or_resume(campaign.id, user_turn.id)
    first = await lifecycle.start_attempt(run.id)
    await lifecycle.set_phase(run.id, GenerationPhase.COMPENSATED)
    await runs.set_status(run.id, "failed", error="first attempt")
    await db_session.commit()

    resumed = await runs.start_or_resume(campaign.id, user_turn.id)
    second = await lifecycle.start_attempt(resumed.id)
    await db_session.commit()

    assert first.attempt == 1
    assert second.attempt == 2
    assert second.phase == GenerationPhase.RECEIVED
    assert second.planned_at is None
    assert second.prepared_at is None
    assert resumed.status == "running"


@pytest.mark.asyncio
async def test_failed_control_request_persists_phase_diagnostics(db_session, monkeypatch):
    import json
    from app.services.turn_saga import TurnSaga
    from app.services.turn_planner import TurnPlanningError

    campaign = await CampaignService(db_session).create_campaign(CampaignCreate(name="Control failure trace"))
    trace = {"phase": "player_intent", "provider": {
        "status": "control_timeout", "attempt": 2, "timeout_seconds": 180,
        "response_model": "PlayerIntentContractDraft",
    }}

    async def fail_plan(self, **kwargs):
        error = TurnPlanningError("player intent interpretation failed: control timeout")
        error.telemetry = trace
        raise error

    monkeypatch.setattr(TurnSaga, "_plan", fail_plan)
    saga = TurnSaga(db_session)
    text = "".join([chunk async for chunk in saga.run_turn_stream(
        campaign.id, TurnCreate(role="user", content="Объясни, откуда знаешь моё имя."),
    )])
    assert "control timeout" in text
    turns = await TurnRepository(db_session).get_history(campaign.id, active_only=False)
    user = next(turn for turn in turns if turn.role == "user")
    assert user.status == "failed"
    assert json.loads(user.context_snapshot)["control_failure"] == trace
