from app.models.turn import ChatMessage
from app.services.planning_context import (
    NARRATOR_EXECUTION_BOUNDARY,
    SCENE_RENDERER_RULES,
    intent_reference_context,
    planning_context,
)
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


def test_intent_extraction_receives_referents_without_world_resolution_policy():
    messages = [
        ChatMessage(
            role="system",
            content=(
                "Be a narrator. Do not move before approval.\n"
                "Location path: Room\nPlayer-owned items: key [id=123]\n"
                "Physically present characters: Kai, Martin\n"
                "Available exits: corridor\nActive conflict: a locked door\n"
            ),
        )
    ]
    assert intent_reference_context(messages) == (
        "Location path: Room\nPlayer-owned items: key [id=123]\n"
        "Physically present characters: Kai, Martin"
    )


def test_outcome_context_preserves_obstacles_without_precluding_scene_changes():
    campaign = "World rule: magic does not exist.\n"
    scene = "[AUTHORITATIVE SCENE STATE]\nActive conflict: locked gate\n"
    source = campaign + NARRATOR_EXECUTION_BOUNDARY + scene + SCENE_RENDERER_RULES
    messages = [ChatMessage(role="system", content=source)]
    assert planning_context(messages) == campaign + scene
    assert messages[0].content == source  # The narrator still receives execution restrictions.
