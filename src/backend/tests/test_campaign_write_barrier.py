import asyncio
from uuid import uuid4

import pytest

from app.services import campaign_write_barrier


def test_same_campaign_shares_one_lock_and_campaigns_are_independent() -> None:
    one, other = uuid4(), uuid4()
    assert campaign_write_barrier.for_campaign(one) is campaign_write_barrier.for_campaign(str(one))
    assert campaign_write_barrier.for_campaign(one) is not campaign_write_barrier.for_campaign(other)


@pytest.mark.asyncio
async def test_memory_writer_queues_behind_the_publication_window() -> None:
    """A post-turn durable write that arrives mid-window lands after the turn commits,
    never between the world-frame capture and its publication assert."""
    campaign_id = uuid4()
    order: list[str] = []

    async def turn() -> None:
        async with campaign_write_barrier.for_campaign(campaign_id):
            order.append("frame-captured")
            await asyncio.sleep(0.05)
            order.append("turn-published")

    async def memory_job() -> None:
        await asyncio.sleep(0.01)
        async with campaign_write_barrier.for_campaign(campaign_id):
            order.append("memory-committed")

    await asyncio.gather(turn(), memory_job())
    assert order == ["frame-captured", "turn-published", "memory-committed"]


@pytest.mark.asyncio
async def test_queued_memory_writers_keep_arrival_order() -> None:
    campaign_id = uuid4()
    order: list[int] = []
    gate = asyncio.Event()

    async def holder() -> None:
        async with campaign_write_barrier.for_campaign(campaign_id):
            await gate.wait()

    async def writer(index: int) -> None:
        async with campaign_write_barrier.for_campaign(campaign_id):
            order.append(index)

    tasks = [asyncio.create_task(holder()), asyncio.create_task(writer(1))]
    await asyncio.sleep(0.01)
    tasks.append(asyncio.create_task(writer(2)))
    await asyncio.sleep(0.01)
    gate.set()
    await asyncio.gather(*tasks)
    assert order == [1, 2]


@pytest.mark.asyncio
async def test_turn_takes_the_barrier_before_its_first_prepare_write(db_session, monkeypatch) -> None:
    """Lock order is barrier → SQLite writer. A turn that wrote its prepare transaction and then
    waited for the barrier deadlocked with a memory job holding the barrier and waiting for SQLite:
    both sat out busy_timeout (30 s) and the memory job failed and re-ran its models."""
    from app.models.campaign import CampaignCreate
    from app.models.turn import TurnCreate
    from app.services.campaign_service import CampaignService
    from app.services.turn_authority_service import TurnAuthorityError, TurnAuthorityService
    from app.services.turn_saga import TurnSaga

    campaign = await CampaignService(db_session).create_campaign(CampaignCreate(name="Lock order"))
    held: list[bool] = []

    async def plan(self, **kwargs):
        return None, {"status": "skipped"}

    async def build(self, **kwargs):
        held.append(campaign_write_barrier.for_campaign(campaign.id).locked())
        raise TurnAuthorityError("stop after the check")

    monkeypatch.setattr(TurnSaga, "_plan", plan)
    monkeypatch.setattr(TurnAuthorityService, "build", build)
    _ = [chunk async for chunk in TurnSaga(db_session).run_turn_stream(
        campaign.id, TurnCreate(role="user", content="Осматриваюсь."))]
    assert held == [True]
    assert not campaign_write_barrier.for_campaign(campaign.id).locked()
