from app.models.turn import ChatMessage
from app.services.prompt_policy import CURRENT_PROMPT_POLICY


def planning_context(messages: list[ChatMessage]) -> str:
    """Keep campaign evidence without the renderer's conflicting prose-only contract.

    The same compiled context is used before and after planning. Instructions such as
    'never expose UUIDs' apply to published fiction, but inventory planning requires UUIDs.
    """
    if not messages:
        return ""
    return messages[0].content.replace(CURRENT_PROMPT_POLICY.narrator_surface_contract, "")
