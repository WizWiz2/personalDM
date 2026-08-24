from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.providers.image_provider import OpenAIImageClient
from app.services.visual_generation import VisualGenerationService


class CloudVisualGenerationService(VisualGenerationService):
    def __init__(self, session: AsyncSession):
        super().__init__(session, client=OpenAIImageClient())

    async def status(self) -> dict:
        enabled = bool(settings.IMAGE_ENABLED and settings.IMAGE_PROVIDER == "cloud")
        connected = enabled and await self._client.health()
        return {
            "enabled": enabled,
            "connected": connected,
            "provider": "cloud",
            "base_url": settings.IMAGE_CLOUD_BASE_URL,
            "model": settings.IMAGE_CLOUD_MODEL,
            "text_encoder": "",
            "lora": "",
        }


def create_visual_generation_service(session: AsyncSession) -> VisualGenerationService:
    if settings.IMAGE_PROVIDER == "cloud":
        return CloudVisualGenerationService(session)
    return VisualGenerationService(session)


__all__ = ["CloudVisualGenerationService", "create_visual_generation_service"]
