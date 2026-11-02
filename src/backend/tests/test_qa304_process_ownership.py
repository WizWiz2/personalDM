"""Two independent SQLite engines must not steal another worker's active turn."""
import asyncio
import sys
from typing import ClassVar
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.application import GameApplication
from app.db.tables import Turn
from app.models.turn import TurnCreate
from app.services.detached_turn_dispatcher import (
    DetachedTurnDispatcher,
    GenerationAlreadyRunningError,
)
from app.services.generation_startup_recovery import recover_interrupted_generations
from app.services.turn_runner import TurnRunner
from tests.test_sqlite_contention_and_recovery import _campaign


class OtherWorker(DetachedTurnDispatcher):
    _tasks: ClassVar[dict] = {}

    @classmethod
    def _has_live_task(cls, campaign_id):
        return False  # another process cannot inspect the first process's Python tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("reader", ["poll", "startup"])
async def test_foreign_live_generation_is_neither_recovered_nor_admitted_twice(file_db, monkeypatch, reader):
    cid = await _campaign(file_db)
    started, finish = asyncio.Event(), asyncio.Event()

    async def stream(self, campaign_id, data, existing_user_turn_id=None):
        started.set()
        await finish.wait()
        yield "ok"

    monkeypatch.setattr(TurnRunner, "run_turn_stream", stream)
    monkeypatch.setattr(GameApplication, "_bind_current_scene", AsyncMock(side_effect=lambda cid, data: data))
    async with file_db() as session:
        accepted = await DetachedTurnDispatcher.submit(cid, TurnCreate(role="user", content="Первый ход"), session)
    task = DetachedTurnDispatcher._tasks[str(cid)]
    try:
        await asyncio.wait_for(started.wait(), 2)
        async with file_db.second_engine()() as session:
            if reader == "startup":
                assert await recover_interrupted_generations(session) == 0
            latest = await OtherWorker.latest_generation(cid, session)
            assert latest.status == "running"
            with pytest.raises(GenerationAlreadyRunningError):
                await OtherWorker.submit(cid, TurnCreate(role="user", content="Второй ход"), session)
        async with file_db() as session:
            users = (await session.execute(select(Turn).where(Turn.campaign_id == str(cid)))).scalars().all()
            assert [(row.id, row.status) for row in users] == [(str(accepted.user_turn.id), "active")]
    finally:
        finish.set()
        await task


@pytest.mark.asyncio
async def test_process_exit_releases_campaign_lease(file_db):
    from app.services.campaign_process_lease import campaign_process_lease

    cid = await _campaign(file_db)
    bind = file_db.kw["bind"]
    code = """
import sys
from types import SimpleNamespace
from app.services.campaign_process_lease import campaign_process_lease
bind = SimpleNamespace(url=SimpleNamespace(database=sys.argv[1]))
with campaign_process_lease(bind, sys.argv[2]):
    print("owned", flush=True)
    sys.stdin.read()
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", code, bind.url.database, str(cid),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert await asyncio.wait_for(process.stdout.readline(), 10) == b"owned\n"
        with pytest.raises(GenerationAlreadyRunningError), campaign_process_lease(
            file_db.second_engine().kw["bind"], cid,
        ):
            pytest.fail("second engine acquired a foreign lease")
    finally:
        process.kill()
        await asyncio.wait_for(process.communicate(), 10)
    with campaign_process_lease(bind, cid):
        async with file_db() as session:
            assert (await session.execute(select(Turn).where(Turn.campaign_id == str(cid)))).scalars().all() == []
