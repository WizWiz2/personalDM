"""F-41: a retry lock cannot roll back already reclaimed stale memory jobs."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import OperationalError

from app.services.post_turn_processor import PostTurnWorker


class _Session:
    def __init__(self):
        self.commit = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


@pytest.mark.asyncio
async def test_stale_recovery_is_committed_before_retry_failed_job_lock():
    worker = PostTurnWorker()
    session = _Session()
    repo = MagicMock()
    repo.recover_stale = AsyncMock(return_value=1)
    repo.requeue_failed = AsyncMock(side_effect=OperationalError(
        "UPDATE post_turn_jobs", {}, Exception("database is locked"),
    ))

    async def exit_after_sweep(waiter, *, timeout):
        waiter.close()
        worker.stop()

    with patch(
        "app.services.post_turn_processor.AsyncSessionLocal",
        return_value=session,
    ), patch(
        "app.services.post_turn_processor.PostTurnJobRepository",
        return_value=repo,
    ), patch(
        "app.services.post_turn_processor.asyncio.wait_for",
        side_effect=exit_after_sweep,
    ):
        await worker._recover_loop()

    # Reclaimed rows stay committed even when the subsequent UPDATE fails.
    session.commit.assert_awaited_once()
    repo.recover_stale.assert_awaited_once()
    repo.requeue_failed.assert_awaited_once()


@pytest.mark.asyncio
async def test_recovery_loop_retries_after_transient_sqlite_lock():
    worker = PostTurnWorker()
    session = _Session()
    repo = MagicMock()
    repo.recover_stale = AsyncMock(side_effect=[
        OperationalError("UPDATE", {}, Exception("database is locked")), 1,
    ])
    repo.requeue_failed = AsyncMock(return_value=0)
    polls = []

    async def next_tick(awaitable, *, timeout):
        awaitable.close()
        polls.append(timeout)
        if len(polls) == 2:
            worker.stop()

    with patch(
        "app.services.post_turn_processor.AsyncSessionLocal",
        return_value=session,
    ), patch(
        "app.services.post_turn_processor.PostTurnJobRepository",
        return_value=repo,
    ), patch(
        "app.services.post_turn_processor.asyncio.wait_for",
        side_effect=next_tick,
    ):
        await worker._recover_loop()
    assert polls == [2.0, 15.0]
    assert repo.recover_stale.await_count == 2
    repo.requeue_failed.assert_awaited_once()
