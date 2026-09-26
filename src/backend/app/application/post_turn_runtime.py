from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.db.tables import PostTurnJob

from app.db.repositories.job_repo import PostTurnJobRepository
from app.services.post_turn_dispatcher import PostTurnDispatcher


async def recover_stale_post_turn_jobs(session: AsyncSession) -> None:
    """Recover durable post-turn jobs at an application boundary."""
    await PostTurnJobRepository(session).recover_stale()
    await session.commit()
    pending_turns = (
        (
            await session.execute(
                select(PostTurnJob.assistant_turn_id)
                .where(PostTurnJob.status == "pending")
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    await session.commit()
    for turn_id in pending_turns:
        PostTurnDispatcher.schedule(session.bind, UUID(turn_id))


async def wait_for_post_turn_idle() -> None:
    """Wait for in-process post-turn work without leaking dispatcher details to UI code."""
    await PostTurnDispatcher.wait_for_idle()


__all__ = ["recover_stale_post_turn_jobs", "wait_for_post_turn_idle"]
