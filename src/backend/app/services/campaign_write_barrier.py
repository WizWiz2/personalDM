import asyncio
from uuid import UUID

_LOCKS: dict[str, asyncio.Lock] = {}


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
        lock = _LOCKS.setdefault(key, asyncio.Lock())
    return lock


__all__ = ["for_campaign"]
