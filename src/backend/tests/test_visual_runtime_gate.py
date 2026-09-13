"""Visual jobs wait for narrative idle and yield to turns."""
from __future__ import annotations

import asyncio

import pytest

from app.services.visual_runtime_gate import VisualRuntimeGate


@pytest.fixture(autouse=True)
def _clear_visual_tasks():
    VisualRuntimeGate._tasks.clear()
    yield
    for task in list(VisualRuntimeGate._tasks):
        task.cancel()
    VisualRuntimeGate._tasks.clear()


@pytest.mark.asyncio
async def test_spawn_runs_when_narrative_idle(monkeypatch: pytest.MonkeyPatch):
    async def idle() -> bool:
        return False

    monkeypatch.setattr(VisualRuntimeGate, "any_narrative_running", staticmethod(idle))
    ran = {"n": 0}

    async def job() -> None:
        ran["n"] += 1

    task = VisualRuntimeGate.spawn(job, name="visual-test-idle")
    await asyncio.wait_for(task, timeout=2)
    assert ran["n"] == 1
    assert not VisualRuntimeGate.is_busy()


@pytest.mark.asyncio
async def test_waits_for_narrative_before_starting(monkeypatch: pytest.MonkeyPatch):
    state = {"narrative": True, "ran": 0}

    async def narrative_running() -> bool:
        return state["narrative"]

    monkeypatch.setattr(
        VisualRuntimeGate,
        "any_narrative_running",
        staticmethod(narrative_running),
    )

    async def job() -> None:
        state["ran"] += 1

    task = VisualRuntimeGate.spawn(job, name="visual-test-wait")
    await asyncio.sleep(0.25)
    assert state["ran"] == 0
    assert VisualRuntimeGate.is_busy()
    state["narrative"] = False
    await asyncio.wait_for(task, timeout=2)
    assert state["ran"] == 1


@pytest.mark.asyncio
async def test_preempt_for_turn_cancels_active_task(monkeypatch: pytest.MonkeyPatch):
    async def idle() -> bool:
        return False

    monkeypatch.setattr(VisualRuntimeGate, "any_narrative_running", staticmethod(idle))
    started = asyncio.Event()

    async def job() -> None:
        started.set()
        await asyncio.sleep(60)

    task = VisualRuntimeGate.spawn(job, name="visual-test-cancel")
    await asyncio.wait_for(started.wait(), timeout=2)
    assert VisualRuntimeGate.is_busy()
    cancelled = VisualRuntimeGate.preempt_for_turn()
    assert cancelled >= 1
    # Runner catches CancelledError and retries; with idle narrative it will restart.
    # Cancel again after a beat, then force-cancel the tracked task set for cleanup.
    await asyncio.sleep(0.05)
    VisualRuntimeGate.preempt_for_turn()
    for tracked in list(VisualRuntimeGate._tasks):
        tracked.cancel()
    await asyncio.sleep(0.05)
