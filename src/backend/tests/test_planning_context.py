from app.models.turn import ChatMessage
from app.services.planning_context import planning_context
from app.services.prompt_policy import CURRENT_PROMPT_POLICY


def test_planning_keeps_evidence_but_not_renderer_instructions():
    context = "[Current Scene: Office]\nGuard is absent.\n"
    refs = "\n[STRUCTURED ACTION REFERENCES]\nKey [id=123]"
    messages = [
        ChatMessage(
            role="system", content=context + CURRENT_PROMPT_POLICY.narrator_surface_contract + refs
        )
    ]
    assert planning_context(messages) == context + refs
