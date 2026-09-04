from __future__ import annotations

import asyncio
import json
import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.db.repositories.generation_lifecycle_repo import GenerationLifecycleRepository
from app.db.tables import Turn
from app.models.jobs import GenerationPhase
from app.services.post_turn_processor import PostTurnProcessor
from app.services.truth_engine_shadow import SemanticResidualShadowService
from app.services.visual_generation_dispatcher import VisualGenerationDispatcher

logger = logging.getLogger(__name__)


class PostTurnDispatcher:
    """Run durable post-turn jobs outside the player's response latency path."""

    _tasks: set[asyncio.Task] = set()
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
                    await GenerationLifecycleRepository(session).set_phase_for_assistant(
                        assistant_turn_id,
                        GenerationPhase.POST_TURN_DONE,
                    )
                    await session.commit()

                    # Experimental TE2 comparison path. It runs only after the legacy post-turn
                    # writer has committed and stores diagnostics in the turn snapshot; it never
                    # publishes TE2 semantic events or changes runtime world state.
                    if settings.TE2_SEMANTIC_SHADOW_ENABLED:
                        try:
                            captured = await SemanticResidualShadowService(session).capture(
                                assistant_turn_id
                            )
                            if captured:
                                await session.commit()
                            else:
                                await session.rollback()
                        except Exception as exc:  # pragma: no cover - live shadow is fail-open
                            await session.rollback()
                            logger.info(
                                "TE2 semantic shadow for %s failed open: %s",
                                assistant_turn_id,
                                exc,
                            )

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
