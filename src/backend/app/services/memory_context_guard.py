from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import UUID

from app.config import settings
from app.db.repositories.narrative_detail_repo import NarrativeDetailRepository
from app.models.turn import ChatMessage
from app.services.context_compiler import ContextCompiler, count_tokens


CompileContext = Callable[..., Awaitable[tuple[list[ChatMessage], dict]]]
_INSTALLED = False
_ORIGINAL_COMPILE_CONTEXT: CompileContext | None = None


async def _compile_context_with_memory_taxonomy(
    self: ContextCompiler,
    campaign_id: UUID,
    acting_character_id: UUID | None = None,
    scene_id: UUID | None = None,
    current_user_content: str | None = None,
    max_budget_override: int | None = None,
):
    if _ORIGINAL_COMPILE_CONTEXT is None:
        raise RuntimeError("Memory context guard was not installed")
    messages, metadata = await _ORIGINAL_COMPILE_CONTEXT(
        self,
        campaign_id,
        acting_character_id,
        scene_id,
        current_user_content,
        max_budget_override,
    )
    metadata = dict(metadata)
    metadata.setdefault("included_narrative_detail_ids", [])
    if not scene_id or not messages:
        return messages, metadata

    details = await NarrativeDetailRepository(self._session).list_recent(
        campaign_id,
        scene_id,
        visibility="public" if acting_character_id else None,
        turn_window=settings.NARRATIVE_DETAIL_TURN_WINDOW,
        max_items=settings.NARRATIVE_DETAIL_MAX_ITEMS,
    )
    if not details:
        return messages, metadata

    section = (
        "[Recent Scene Texture — transient, non-canon]\n"
        + "".join(f"- {detail.text}\n" for detail in details)
        + "Use these details only for immediate visual and sensory continuity. "
        "Do not turn them into stable personality traits, beliefs, promises, "
        "world laws or campaign facts unless a later authoritative outcome "
        "explicitly establishes such a change.\n"
    )
    section_tokens = count_tokens(section)
    budget_max = int(metadata.get("token_budget_max") or 0)
    budget_used = int(metadata.get("token_budget_used") or 0)
    if budget_max and budget_used + section_tokens >= budget_max:
        metadata["narrative_details_omitted_for_budget"] = len(details)
        return messages, metadata

    first = messages[0]
    messages = [
        ChatMessage(role=first.role, content=f"{first.content}\n\n{section}"),
        *messages[1:],
    ]
    metadata["token_budget_used"] = budget_used + section_tokens
    metadata["included_narrative_detail_ids"] = [
        str(detail.id) for detail in details
    ]
    layers = list(metadata.get("included_layers") or [])
    layers.append("layer_1b_recent_narrative_details")
    metadata["included_layers"] = layers
    return messages, metadata


def install() -> None:
    global _INSTALLED, _ORIGINAL_COMPILE_CONTEXT
    if _INSTALLED:
        return
    _ORIGINAL_COMPILE_CONTEXT = ContextCompiler.compile_context
    ContextCompiler.compile_context = _compile_context_with_memory_taxonomy
    _INSTALLED = True
