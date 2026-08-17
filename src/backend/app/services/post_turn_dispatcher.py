from __future__ import annotations

import asyncio
import json
import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.tables import Turn
from app.services.post_turn_processor import PostTurnProcessor
from app.services.visual_generation_dispatcher import VisualGenerationDispatcher

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
                    # TurnSaga records authoritative new-NPC ids in the assistant snapshot
                    # before this dispatcher runs. Visual generation reads that evidence instead
                    # of re-inferring NPCs from prose, so portraits are created exactly once for
                    # actual materialized characters and never for hallucinated mentions.
                    row = await session.get(Turn, str(assistant_turn_id))
                    if row and row.context_snapshot:
                        try:
                            snapshot = json.loads(row.context_snapshot)
                        except (json.JSONDecodeError, TypeError):
                            snapshot = {}
                        materialization = (
                            snapshot.get("turn_materialization")
                            if isinstance(snapshot, dict)
                            else None
                        ) or {}
                        ids: list[UUID] = []
                        for value in materialization.get("introduced_character_ids") or []:
                            try:
                                ids.append(UUID(str(value)))
                            except (ValueError, TypeError):
                                continue
                        VisualGenerationDispatcher.schedule_character_portraits(ids)
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
