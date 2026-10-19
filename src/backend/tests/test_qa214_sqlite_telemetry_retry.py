"""F-38: a transient SQLite writer lock must not discard telemetry."""
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.exc import OperationalError

from app.services.llm_usage_tracker import LLMUsageContext, _flush


class _FakeSession:
    def __init__(self, attempts):
        self.attempts = attempts

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def commit(self):
        self.attempts[0] += 1
        if self.attempts[0] < 3:
            raise OperationalError("INSERT INTO llm_usage_events", {}, Exception("database is locked"))


@pytest.mark.asyncio
async def test_telemetry_retries_transient_sqlite_writer_lock():
    attempts = [0]
    writer = AsyncMock()
    context = LLMUsageContext(campaign_id=uuid4(), user_turn_id=uuid4())
    with patch("app.services.llm_usage_tracker.asyncio.sleep", new_callable=AsyncMock) as sleep:
        await _flush(
            lambda: _FakeSession(attempts),
            "usage",
            context,
            [{"model": "test"}],
            writer,
        )
    assert attempts[0] == 3
    assert writer.await_count == 3
    assert sleep.await_count == 2


@pytest.mark.asyncio
async def test_telemetry_does_not_retry_nontransient_sql_error():
    attempts = [0]
    writer = AsyncMock(side_effect=OperationalError("INSERT", {}, Exception("no such table")))
    context = LLMUsageContext(campaign_id=uuid4(), user_turn_id=uuid4())
    with patch("app.services.llm_usage_tracker.asyncio.sleep", new_callable=AsyncMock) as sleep:
        await _flush(lambda: _FakeSession(attempts), "usage", context, [{}], writer)
    assert writer.await_count == 1
    sleep.assert_not_awaited()
