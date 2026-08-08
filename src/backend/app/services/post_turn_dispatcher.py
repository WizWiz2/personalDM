from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.services.post_turn_processor import PostTurnProcessor

logger = logging.getLogger(__name__)


class PostTurnDispatcher:
    """Run durable post-turn jobs outside the player's response latency path."""

    _tasks: set[asyncio.Task] = set()
    # Deterministic tests may opt into awaiting the exact same background task. Production
    # never changes this value and therefore never waits for Registrar/Scribe/Curator.
    wait_inline_for_tests: bool = False

    @classmethod
    def schedule(cls, bind, assistant_turn_id: UUID) -> asyncio.Task | None:
        if bind is None:
            return None

        async def run() -> None:
            factory = async_sessionmaker(
                bind=bind,
                expire_on_commit=False,
                autoflush=False,
            )
            try:
                async with factory() as session:
                    await PostTurnProcessor(session).process_turn(assistant_turn_id)
            except Exception as exc:  # pragma: no cover - durable job records keep evidence
                logger.info(
                    "Post-turn processing for %s deferred after failure: %s",
                    assistant_turn_id,
                    exc,
                )

        task = asyncio.create_task(
            run(),
            name=f"post-turn-{assistant_turn_id}",
        )
        cls._tasks.add(task)
        task.add_done_callback(cls._tasks.discard)
        return task

    @classmethod
    async def wait_for_idle(cls) -> None:
        """Test/debug seam; production UI never waits for background memory."""
        tasks = tuple(cls._tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


__all__ = ["PostTurnDispatcher"]
