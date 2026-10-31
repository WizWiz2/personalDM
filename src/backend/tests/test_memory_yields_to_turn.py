"""A post-turn memory job never starts a provider call while a turn is in flight; it resumes
after publication. A job inside the campaign barrier never waits (no #275-style deadlock)."""
import asyncio
from types import SimpleNamespace

import pytest

from app.services import campaign_write_barrier
from app.services.role_model_router import RoleModelRouter
from app.services.turn_saga import active_tasks


def _router(calls):
    router = RoleModelRouter(config_repo=None)

    async def once(*_args, **_kwargs):
        calls.append(asyncio.get_running_loop().time())
        return {}
    router._generate_json_once = once
    return router


async def _turn(release: asyncio.Event):
    await release.wait()


@pytest.mark.asyncio
async def test_background_call_waits_for_the_turn_to_publish():
    calls, release = [], asyncio.Event()
    turn = asyncio.create_task(_turn(release))
    active_tasks["c1"] = turn
    try:
        async def job():
            campaign_write_barrier.background_job.set(True)
            await _router(calls).generate_json(SimpleNamespace(last_telemetry={}), SimpleNamespace(config=None, api_key=None, role=SimpleNamespace(value="scribe"), source="t"), [])
        pending = asyncio.create_task(job())
        await asyncio.sleep(0.5)
        assert calls == []  # the turn owns the provider
        release.set()
        await turn
        del active_tasks["c1"]
        await asyncio.wait_for(pending, 2)
        assert len(calls) == 1
    finally:
        active_tasks.pop("c1", None)


@pytest.mark.asyncio
async def test_job_holding_the_barrier_does_not_wait_for_a_turn():
    calls, release = [], asyncio.Event()
    active_tasks["c2"] = asyncio.create_task(_turn(release))
    try:
        async def job():
            campaign_write_barrier.background_job.set(True)
            async with campaign_write_barrier.for_campaign("c2"):
                await _router(calls).generate_json(SimpleNamespace(last_telemetry={}), SimpleNamespace(config=None, api_key=None, role=SimpleNamespace(value="scribe"), source="t"), [])
        await asyncio.wait_for(job(), 2)
        assert len(calls) == 1
    finally:
        release.set()
        active_tasks.pop("c2", None)


@pytest.mark.asyncio
async def test_a_turn_call_is_never_delayed():
    calls, release = [], asyncio.Event()
    active_tasks["c3"] = asyncio.create_task(_turn(release))
    try:
        await asyncio.wait_for(_router(calls).generate_json(SimpleNamespace(last_telemetry={}), SimpleNamespace(config=None, api_key=None, role=SimpleNamespace(value="scribe"), source="t"), []), 2)
        assert len(calls) == 1
    finally:
        release.set()
        active_tasks.pop("c3", None)
