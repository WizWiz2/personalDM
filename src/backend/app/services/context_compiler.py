from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.turn import ChatMessage
from app.services.base_context_compiler import (
    ContextCompiler as BaseContextCompiler,
)
from app.services.base_context_compiler import count_tokens
from app.services.context_pipeline import (
    ContextPipeline,
    ContextProvider,
    ContextRequest,
    NarrativeDetailsContextProvider,
    SceneStateContextProvider,
)


class ContextCompiler(BaseContextCompiler):
    """Compile the base prompt and enrich it through explicit ordered providers."""

    DEFAULT_PROVIDER_NAMES = (
        SceneStateContextProvider.name,
        NarrativeDetailsContextProvider.name,
    )

    def __init__(
        self,
        session: AsyncSession,
        context_providers: Sequence[ContextProvider] | None = None,
    ):
        super().__init__(session)
        providers = (
            tuple(context_providers)
            if context_providers is not None
            else (
                SceneStateContextProvider(session),
                NarrativeDetailsContextProvider(session, count_tokens),
            )
        )
        self._context_pipeline = ContextPipeline(providers)

    @property
    def context_provider_names(self) -> tuple[str, ...]:
        return self._context_pipeline.provider_names

    async def compile_context(
        self,
        campaign_id: UUID,
        acting_character_id: UUID | None = None,
        scene_id: UUID | None = None,
        current_user_content: str | None = None,
        max_budget_override: int | None = None,
    ) -> tuple[list[ChatMessage], dict]:
        messages, metadata = await super().compile_context(
            campaign_id=campaign_id,
            acting_character_id=acting_character_id,
            scene_id=scene_id,
            current_user_content=current_user_content,
            max_budget_override=max_budget_override,
        )
        return await self._context_pipeline.enrich(
            ContextRequest(
                campaign_id=campaign_id,
                acting_character_id=acting_character_id,
                scene_id=scene_id,
                current_user_content=current_user_content,
                max_budget_override=max_budget_override,
            ),
            messages,
            metadata,
        )


__all__ = ["ContextCompiler", "count_tokens"]
