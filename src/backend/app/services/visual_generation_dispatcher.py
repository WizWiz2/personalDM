from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from uuid import UUID

from app.config import settings
from app.db.engine import AsyncSessionLocal
from app.services.visual_provider_factory import create_visual_generation_service
from app.services.visual_runtime_gate import VisualRuntimeGate

logger = logging.getLogger(__name__)


class VisualGenerationDispatcher:
    """Best-effort visual work that must never block or invalidate game state.

    All GPU visual jobs go through ``VisualRuntimeGate`` so they wait for narrative
    idle time and yield VRAM when a turn starts.
    """

    @staticmethod
    def schedule_session_zero(campaign_id: UUID, character_id: UUID) -> None:
        if not settings.IMAGE_ENABLED or settings.IMAGE_PROVIDER == "off":
            return
        VisualGenerationDispatcher.schedule(
            lambda: VisualGenerationDispatcher._session_zero(campaign_id, character_id),
            name=f"visual-session-zero-{campaign_id}",
        )

    @staticmethod
    def schedule_character_portraits(character_ids: Iterable[UUID]) -> None:
        if not settings.IMAGE_ENABLED or settings.IMAGE_PROVIDER == "off":
            return
        ids = tuple(dict.fromkeys(character_ids))
        if not ids:
            return
        VisualGenerationDispatcher.schedule(
            lambda: VisualGenerationDispatcher._character_portraits(ids),
            name=f"visual-character-portraits-{ids[0]}",
        )

    @staticmethod
    def schedule(factory: Callable[[], Awaitable[None]], *, name: str) -> None:
        if not settings.IMAGE_ENABLED or settings.IMAGE_PROVIDER == "off":
            return
        VisualRuntimeGate.spawn(factory, name=name)

    @staticmethod
    async def _session_zero(campaign_id: UUID, character_id: UUID) -> None:
        async with AsyncSessionLocal() as session:
            service = create_visual_generation_service(session)
            try:
                await service.generate_campaign_cover(campaign_id)
                await session.commit()
            except Exception as exc:
                await session.rollback()
                logger.info("Campaign cover generation skipped: %s", exc)

        async with AsyncSessionLocal() as session:
            service = create_visual_generation_service(session)
            try:
                await service.generate_character_portrait(character_id)
                await session.commit()
            except Exception as exc:
                await session.rollback()
                logger.info("Player portrait generation skipped: %s", exc)

    @staticmethod
    async def _character_portraits(character_ids: tuple[UUID, ...]) -> None:
        for character_id in character_ids:
            async with AsyncSessionLocal() as session:
                service = create_visual_generation_service(session)
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
