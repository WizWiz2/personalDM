from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

from sqlalchemy import select

from app.db.engine import AsyncSessionLocal
from app.db.tables import GenerationRun

logger = logging.getLogger(__name__)

T = TypeVar("T")
JobFactory = Callable[[], Awaitable[T]]


class VisualRuntimeGate:
    """Serialize GPU-heavy visuals against narrative turns on small VRAM.

    Rules:
    - visuals start only when no GenerationRun is ``running``;
    - starting a turn preempts in-flight visuals (Cancel) so LLM can reclaim VRAM;
    - preempted jobs retry after the narrative run goes idle (best-effort).
    """

    _tasks: set[asyncio.Task] = set()
    _lock: asyncio.Lock | None = None

    @classmethod
    def _ensure_lock(cls) -> asyncio.Lock:
        if cls._lock is None:
            cls._lock = asyncio.Lock()
        return cls._lock

    @classmethod
    def is_busy(cls) -> bool:
        return any(not task.done() for task in cls._tasks)

    @classmethod
    def active_count(cls) -> int:
        return sum(1 for task in cls._tasks if not task.done())

    @classmethod
    def preempt_for_turn(cls) -> int:
        """Cancel in-flight visual work so a narrative turn can own the GPU."""
        cancelled = 0
        for task in list(cls._tasks):
            if task.done():
                continue
            task.cancel()
            cancelled += 1
        if cancelled:
            logger.info("Preempted %s visual job(s) for narrative turn", cancelled)
        return cancelled

    @classmethod
    async def any_narrative_running(cls) -> bool:
        async with AsyncSessionLocal() as session:
            row = (
                await session.execute(
                    select(GenerationRun.id)
                    .where(GenerationRun.status == "running")
                    .limit(1)
                )
            ).scalar_one_or_none()
            return row is not None

    @classmethod
    async def wait_until_narrative_idle(cls, *, poll_seconds: float = 0.4) -> None:
        while await cls.any_narrative_running():
            await asyncio.sleep(poll_seconds)

    @classmethod
    def spawn(cls, factory: JobFactory, *, name: str) -> asyncio.Task:
        """Run ``factory`` when narrative is idle; retry if a turn cancels it."""

        async def runner() -> None:
            while True:
                await cls.wait_until_narrative_idle()
                try:
                    await factory()
                    return
                except asyncio.CancelledError:
                    logger.info("Visual job %s yielded to narrative turn; will retry", name)
                    await asyncio.sleep(0.2)
                    await cls.wait_until_narrative_idle()
                    continue
                except Exception:
                    logger.exception("Visual job %s failed", name)
                    return

        task = asyncio.create_task(runner(), name=name)
        cls._tasks.add(task)

        def _done(completed: asyncio.Task) -> None:
            cls._tasks.discard(completed)
            try:
                completed.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

        task.add_done_callback(_done)
        return task


__all__ = ["VisualRuntimeGate"]
