"""Serialize local Ollama work across models; live turns precede queued memory jobs."""

from __future__ import annotations

import asyncio
import heapq
import itertools
import time
import weakref
from contextlib import asynccontextmanager
from contextvars import ContextVar
from urllib.parse import urlparse

from app.config import settings


class LocalInferenceQueueTimeout(RuntimeError):
    pass


class _Queue:
    def __init__(self):
        self.busy = False
        self.pending = []
        self.sequence = itertools.count()
        self.interactive = 0

    def release(self):
        while self.pending:
            if self.interactive and self.pending[0][0]:
                self.busy = False
                return
            _, _, waiter = heapq.heappop(self.pending)
            if not waiter.done():
                waiter.set_result(None)
                return
        self.busy = False


_queues = weakref.WeakKeyDictionary()
_interactive_counts = weakref.WeakKeyDictionary()
background_inference = ContextVar("background_inference", default=False)


def begin_interactive():
    loop = asyncio.get_running_loop()
    count = _interactive_counts.get(loop, 0) + 1
    _interactive_counts[loop] = count
    for queue in _queues.get(loop, {}).values():
        queue.interactive = count


def end_interactive():
    loop = asyncio.get_running_loop()
    count = max(0, _interactive_counts.get(loop, 0) - 1)
    _interactive_counts[loop] = count
    for queue in _queues.get(loop, {}).values():
        queue.interactive = count
        if not queue.busy:
            queue.busy = True
            queue.release()


@asynccontextmanager
async def local_inference_slot(base_url: str, *, background: bool = False):
    parsed = urlparse(base_url)
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"} or parsed.port not in {None, 11434}:
        yield 0.0
        return
    loop = asyncio.get_running_loop()
    # All loopback aliases and models share the same local inference server/GPU.
    queues = _queues.setdefault(loop, {})
    queue = queues.setdefault(parsed.port or 11434, _Queue())
    queue.interactive = _interactive_counts.get(loop, 0)
    background = background or background_inference.get()
    started = time.monotonic()
    if queue.busy or (background and queue.interactive):
        waiter = loop.create_future()
        entry = (int(background), next(queue.sequence), waiter)
        heapq.heappush(queue.pending, entry)
        try:
            # A reservation can begin AFTER this job enters the queue. Memory must
            # remain deferred for the whole live turn, not fail on an entry-time
            # snapshot of the reservation. Active inference has its own budget.
            # A live turn must also be able to wait for one already running call
            # using the configured slow-CPU transport/control budgets.
            timeout = None if background else max(
                1.0,
                settings.LOCAL_LLM_QUEUE_TIMEOUT_SECONDS,
                settings.LLM_HTTP_TIMEOUT_SECONDS,
                settings.CONTROL_LLM_TIMEOUT_SECONDS,
            )
            async with asyncio.timeout(timeout):
                await waiter
        except BaseException as exc:
            # Cancellation can race with a grant. In that case pass the slot on.
            if waiter.done() and not waiter.cancelled():
                queue.release()
            elif entry in queue.pending:
                queue.pending.remove(entry)
                heapq.heapify(queue.pending)
            if isinstance(exc, TimeoutError):
                raise LocalInferenceQueueTimeout("Local model queue wait budget exceeded") from exc
            raise
    else:
        queue.busy = True
    try:
        yield round((time.monotonic() - started) * 1000)
    finally:
        queue.release()
