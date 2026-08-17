from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from uuid import UUID

from app.config import settings
from app.db.engine import AsyncSessionLocal
from app.services.visual_generation import VisualGenerationService

logger = logging.getLogger(__name__)


class VisualGenerationDispatcher:
    """Best-effort visual work that must never block or invalidate game state."""

    @staticmethod
    def schedule_session_zero(campaign_id: UUID, character_id: UUID) -> None:
        if not settings.IMAGE_ENABLED:
            return
        VisualGenerationDispatcher._spawn(
            VisualGenerationDispatcher._session_zero(campaign_id, character_id),
            name=f"visual-session-zero-{campaign_id}",
        )

    @staticmethod
    def schedule_character_portraits(character_ids: Iterable[UUID]) -> None:
        if not settings.IMAGE_ENABLED:
            return
        ids = tuple(dict.fromkeys(character_ids))
        if not ids:
            return
        VisualGenerationDispatcher._spawn(
            VisualGenerationDispatcher._character_portraits(ids),
            name=f"visual-character-portraits-{ids[0]}",
        )

    @staticmethod
    def _spawn(coro, *, name: str) -> None:
        task = asyncio.create_task(coro, name=name)

        def _done(completed: asyncio.Task) -> None:
            try:
                completed.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # pragma: no cover - defensive background boundary
                logger.info("Background visual generation deferred: %s", exc)

        task.add_done_callback(_done)

    @staticmethod
    async def _session_zero(campaign_id: UUID, character_id: UUID) -> None:
        # Portrait first because it can immediately become a reference for later scene art.
        async with AsyncSessionLocal() as session:
            service = VisualGenerationService(session)
            try:
                await service.generate_character_portrait(character_id)
                await session.commit()
            except Exception as exc:
                await session.rollback()
                logger.info("Player portrait generation skipped: %s", exc)

        async with AsyncSessionLocal() as session:
            service = VisualGenerationService(session)
            try:
                await service.generate_campaign_cover(campaign_id)
                await session.commit()
            except Exception as exc:
                await session.rollback()
                logger.info("Campaign cover generation skipped: %s", exc)

    @staticmethod
    async def _character_portraits(character_ids: tuple[UUID, ...]) -> None:
        # Generate sequentially: an 8 GB GPU should not run several Klein jobs in parallel.
        for character_id in character_ids:
            async with AsyncSessionLocal() as session:
                service = VisualGenerationService(session)
                try:
                    await service.generate_character_portrait(character_id)
                    await session.commit()
                except Exception as exc:
                    await session.rollback()
                    logger.info(
                        "NPC portrait generation skipped for %s: %s",
                        character_id,
                        exc,
                    )


__all__ = ["VisualGenerationDispatcher"]
