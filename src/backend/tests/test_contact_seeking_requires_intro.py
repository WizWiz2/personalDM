from app.models.player_intent import PlayerIntentContract
from app.models.turn import ChatMessage
from app.services.turn_outcome_resolver import TurnOutcomeDecision, TurnOutcomeResolver


def _contract(*, addressed: bool) -> PlayerIntentContract:
    return PlayerIntentContract.model_validate(
        {
            "summary": "seek local people",
            "actions": [
                {
                    "action_type": "observation",
                    "intent": "look for attendants",
                }
            ],
            "addressed_response_requested": addressed,
        }
    )


def _empty_decision() -> TurnOutcomeDecision:
    return TurnOutcomeDecision.model_validate(
        {
            "action_outcomes": [
                {
                    "action_index": 0,
                    "resolution": "auto_success",
                    "observable_outcome": "Никого нет.",
                }
            ],
            "npc_introductions": [],
            "resolution": "success",
            "observable_consequences": ["Никого нет."],
            "character_beats": [],
        }
    )


def test_requires_intro_when_seeking_and_player_only_presence():
    context = [
        ChatMessage(role="system", content="Physically present characters: Эйдан"),
    ]
    assert TurnOutcomeResolver._requires_contact_introduction(
        _contract(addressed=True), context, _empty_decision()
    )


def test_does_not_require_intro_when_not_addressed():
    context = [
        ChatMessage(role="system", content="Physically present characters: Эйдан"),
    ]
    assert not TurnOutcomeResolver._requires_contact_introduction(
        _contract(addressed=False), context, _empty_decision()
    )
