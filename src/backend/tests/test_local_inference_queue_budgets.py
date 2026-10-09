"""Scheduling regressions for slow real inference and turn-long memory deferral."""
import asyncio

import pytest

from app.config import settings
from app.providers.local_inference_queue import (
    begin_interactive, end_interactive, local_inference_slot,
)


@pytest.mark.asyncio
async def test_live_request_can_wait_for_configured_active_inference_budget(monkeypatch):
    monkeypatch.setattr(settings, "LOCAL_LLM_QUEUE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(settings, "CONTROL_LLM_TIMEOUT_SECONDS", 1.1)
    monkeypatch.setattr(settings, "LLM_HTTP_TIMEOUT_SECONDS", 1.1)
    entered = asyncio.Event()

    async def current_inference():
        async with local_inference_slot("http://localhost:11434/v1", background=True):
            entered.set()
            await asyncio.sleep(1.02)

    owner = asyncio.create_task(current_inference())
    await entered.wait()
    async with local_inference_slot("http://127.0.0.1:11434/v1") as waited_ms:
        assert waited_ms >= 1000
    await owner


@pytest.mark.asyncio
async def test_memory_queued_before_reservation_does_not_expire_during_live_turn(monkeypatch):
    monkeypatch.setattr(settings, "LOCAL_LLM_QUEUE_TIMEOUT_SECONDS", 0.01)
    completed = []

    async def memory():
        async with local_inference_slot("http://localhost:11434/v1", background=True):
            completed.append("memory")

    async with local_inference_slot("http://localhost:11434/v1"):
        task = asyncio.create_task(memory())
        await asyncio.sleep(0)
        begin_interactive()
    try:
        await asyncio.sleep(1.02)
        assert not task.done()
        async with local_inference_slot("http://localhost:11434/v1"):
            completed.append("player")
    finally:
        end_interactive()
    await asyncio.wait_for(task, 1)
    assert completed == ["player", "memory"]


@pytest.mark.asyncio
async def test_cancelled_deferred_memory_does_not_steal_next_live_slot():
    begin_interactive()

    async def memory():
        async with local_inference_slot("http://localhost:11434/v1", background=True):
            raise AssertionError("Deferred memory must not run")

    task = asyncio.create_task(memory())
    try:
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with asyncio.timeout(1):
            async with local_inference_slot("http://localhost:11434/v1"):
                pass
    finally:
        end_interactive()
