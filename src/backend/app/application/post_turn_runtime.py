from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.job_repo import PostTurnJobRepository
from app.services.post_turn_dispatcher import PostTurnDispatcher


async def recover_stale_post_turn_jobs(session: AsyncSession) -> None:
    """Recover durable post-turn jobs at an application boundary."""
    await PostTurnJobRepository(session).recover_stale()
    await session.commit()


async def wait_for_post_turn_idle() -> None:
    """Wait for in-process post-turn work without leaking dispatcher details to UI code."""
    await PostTurnDispatcher.wait_for_idle()


__all__ = ["recover_stale_post_turn_jobs", "wait_for_post_turn_idle"]
