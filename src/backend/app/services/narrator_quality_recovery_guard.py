from __future__ import annotations

from app.config import settings

_INSTALLED = False


def narrator_context_budget(context_window: int) -> int:
    """Budget final Narrator context after Planner has already completed its separate call."""
    safety_margin = int(context_window * settings.SAFETY_MARGIN_PERCENT)
    return max(
        512,
        int(context_window) - settings.RESPONSE_RESERVE_TOKENS - safety_margin,
    )


def install() -> None:
    """Give the narrator the full context budget once planning has finished."""
    global _INSTALLED
    if _INSTALLED:
        return

    from app.services.context_compiler import ContextCompiler
    from app.services.turn_saga import TurnSaga

    async def narration_budget_compile(
        self,
        campaign_id,
        turn_create,
        scene_id,
        primary_config,
    ):
        max_budget_override = None
        if turn_create.acting_character_id is None:
            max_budget_override = narrator_context_budget(primary_config.context_window)
        compiler = ContextCompiler(self._session)
        messages, metadata = await compiler.compile_context(
            campaign_id=campaign_id,
            acting_character_id=turn_create.acting_character_id,
            scene_id=scene_id,
            current_user_content=turn_create.content,
            max_budget_override=max_budget_override,
        )
        metadata = dict(metadata)
        if turn_create.acting_character_id is None:
            metadata["planner_reserve_removed_from_narrator_budget"] = True
            metadata["final_narrator_context_budget"] = max_budget_override
        return (
            self._reserve_current_user(messages, metadata, turn_create.content),
            compiler,
            max_budget_override,
        )

    TurnSaga._compile = narration_budget_compile
    _INSTALLED = True


__all__ = ["install", "narrator_context_budget"]
