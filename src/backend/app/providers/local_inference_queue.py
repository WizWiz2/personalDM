"""Serialize local Ollama work across models; live turns precede queued memory jobs."""

from __future__ import annotations

import asyncio
import heapq
import itertools
import time
import weakref
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from app.config import settings


class LocalInferenceQueueTimeout(RuntimeError):
    pass


class _Queue:
    def __init__(self):
        self.busy = False
        self.pending = []
        self.sequence = itertools.count()

    def release(self):
        while self.pending:
            _, _, waiter = heapq.heappop(self.pending)
            if not waiter.done():
                waiter.set_result(None)
                return
        self.busy = False


_queues = weakref.WeakKeyDictionary()


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
    started = time.monotonic()
    if queue.busy:
        waiter = loop.create_future()
        entry = (int(background), next(queue.sequence), waiter)
        heapq.heappush(queue.pending, entry)
        try:
            async with asyncio.timeout(max(1.0, settings.LOCAL_LLM_QUEUE_TIMEOUT_SECONDS)):
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
