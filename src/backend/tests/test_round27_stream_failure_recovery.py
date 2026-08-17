from uuid import uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.job_repo import GenerationRunRepository
from app.db.repositories.turn_repo import TurnRepository
from app.models.campaign import CampaignCreate
from app.models.turn import TurnCreate
from app.services.detached_turn_dispatcher import DetachedTurnDispatcher
from app.services.turn_runner import TurnRunner


@pytest.mark.asyncio
async def test_remote_protocol_error_cannot_leave_detached_turn_running(test_engine, monkeypatch):
    """Round 27: an incomplete chunked read must release the campaign for the next input."""
    campaign_id = uuid4()
    factory = async_sessionmaker(
        bind=test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )

    async with factory() as session:
        await CampaignRepository(session).create(
            campaign_id,
            CampaignCreate(name="Round 27 stream recovery"),
        )
        user_turn = await TurnRepository(session).create(
            campaign_id,
            TurnCreate(role="user", content="Когда вы это заметили?"),
        )
        generation = await GenerationRunRepository(session).start_or_resume(
            campaign_id,
            user_turn.id,
        )
        await session.commit()

    async def broken_stream(self, campaign_id, data, existing_user_turn_id=None):
        if False:  # pragma: no cover - keeps this an async generator like production
            yield ""
        request = httpx.Request("POST", "http://ollama.invalid/api/chat")
        raise httpx.RemoteProtocolError(
            "peer closed connection without sending complete message body (incomplete chunked read)",
            request=request,
        )

    monkeypatch.setattr(TurnRunner, "run_turn_stream", broken_stream)

    await DetachedTurnDispatcher._run(
        factory,
        campaign_id,
        "narrative",
        TurnCreate(role="user", content="Когда вы это заметили?"),
        user_turn.id,
    )

    async with factory() as session:
        run = await GenerationRunRepository(session).get_by_user_turn(user_turn.id)
        persisted_user = await TurnRepository(session).get_by_id(user_turn.id)
        latest = await DetachedTurnDispatcher.latest_generation(campaign_id, session)

        assert run is not None
        assert run.id == generation.id
        assert run.status == "failed"
        assert "incomplete chunked read" in (run.error or "")
        assert persisted_user is not None
        assert persisted_user.status == "failed"
        assert latest is not None
        assert latest.status == "failed"
        assert not DetachedTurnDispatcher._has_live_task(campaign_id)
