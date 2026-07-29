import pytest

from app.models.turn import ChatMessage
from app.services.turn_planner import TurnPlan, TurnPlanner, TurnPlanningError


def sample_plan() -> TurnPlan:
    return TurnPlan(
        player_intent="Question the guard without making a commitment.",
        resolution="conversation",
        observable_consequences=["The guard answers cautiously."],
        character_beats=["The guard protects their superior."],
        canon_constraints=["The player has not accepted the offer."],
        new_fact_candidates=["The eastern gate closes at dusk."],
        narration_guidance=["Keep the exchange compact."],
        ending_hook="The guard waits for a response.",
    )


def test_inject_plan_preserves_history_and_keeps_plan_internal():
    messages = [
        ChatMessage(role="system", content="Campaign truth"),
        ChatMessage(role="user", content="I question the guard."),
    ]

    result = TurnPlanner.inject_plan(messages, sample_plan())

    assert len(result) == len(messages)
    assert result[1] == messages[1]
    assert "[APPROVED TURN PLAN]" in result[0].content
    assert '"resolution": "conversation"' in result[0].content
    assert "The plan itself does not update canon" in result[0].content


def test_planning_messages_reframe_narrator_context_as_structured_work():
    messages = [
        ChatMessage(role="system", content="Campaign truth"),
        ChatMessage(role="user", content="I question the guard."),
    ]

    result = TurnPlanner.planning_messages(messages)

    assert "[TURN PLANNER]" in result[0].content
    assert "[CAMPAIGN CONTEXT]" in result[0].content
    assert result[1] == messages[1]


def test_empty_context_is_rejected():
    with pytest.raises(TurnPlanningError):
        TurnPlanner.planning_messages([])
