import asyncio
from contextvars import ContextVar
from uuid import UUID

_LOCKS: dict[str, "_OwnedLock"] = {}
# The single registry of in-flight turns (TurnSaga owns it): campaign id -> turn task.
active_tasks: dict[str, asyncio.Task] = {}
# Set by post-turn memory work: its provider calls yield to in-flight turns.
background_job: ContextVar[bool] = ContextVar("background_job", default=False)


class _OwnedLock(asyncio.Lock):
    owner: asyncio.Task | None = None

    async def acquire(self):
        await super().acquire()
        self.owner = asyncio.current_task()
        return True

    def release(self):
        self.owner = None
        super().release()


def for_campaign(campaign_id: UUID | str) -> asyncio.Lock:
    """Campaign-scoped write barrier for the prepare→publish window.

    A turn captures the physical world frame after structured preparation and asserts it
    unchanged at publication. Post-turn memory jobs may auto-commit MOVEMENT or presence
    proposals that mutate exactly those fingerprinted rows. This barrier serializes the
    two writers per campaign on the single application event loop:

    - TurnSaga holds the lock from world-frame capture through the publication commit
      (and compensation on failure, via the saga's finally).
    - PostTurnProcessor holds it only around its durable write phase, never around the
      semantic LLM calls, so memory work queues behind turns instead of poisoning them.

    asyncio.Lock is FIFO, so deferred memory jobs commit in arrival order.
    """
    key = str(campaign_id)
    lock = _LOCKS.get(key)
    if lock is None:
        lock = _LOCKS.setdefault(key, _OwnedLock())
    return lock


async def yield_to_turns() -> None:
    """Memory work starts no provider call while any turn is in flight (one shared provider).

    A job that holds a barrier never waits: the turn may be queued behind it (#275).
    """
    current = asyncio.current_task()
    if not background_job.get() or any(lock.owner is current for lock in _LOCKS.values()):
        return
    while any(not task.done() for task in active_tasks.values()):
        await asyncio.sleep(0.2)


__all__ = ["active_tasks", "background_job", "for_campaign", "yield_to_turns"]
