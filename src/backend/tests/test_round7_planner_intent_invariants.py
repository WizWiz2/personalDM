import pytest
from pydantic import ValidationError

from app.services.turn_authority_planner import CoordinatedTurnPlan, TurnAuthorityPlanner


def test_requires_check_is_not_a_legal_systemless_step():
    with pytest.raises(ValidationError):
        CoordinatedTurnPlan.model_validate(
            {
                "player_intent": "Поговорить с трактирщиком",
                "resolution": "sequence",
                "action_sequence": {
                    "steps": [
                        {
                            "action_type": "interaction",
                            "intent": "Поговорить с трактирщиком о магах",
                            "resolution": "requires_check",
                        }
                    ]
                },
            }
        )


def test_auto_success_world_action_needs_renderable_typed_outcome():
    with pytest.raises(ValidationError, match="observable_outcome"):
        CoordinatedTurnPlan.model_validate(
            {
                "player_intent": "Осмотреть комнату",
                "resolution": "sequence",
                "action_sequence": {
                    "steps": [
                        {
                            "action_type": "observation",
                            "intent": "Осмотреть комнату",
                            "resolution": "auto_success",
                            "safe_mundane": True,
                            "transition": {"required": False},
                        }
                    ]
                },
            }
        )


def test_auto_success_movement_requires_structured_location_transition():
    with pytest.raises(ValidationError, match="location_transition"):
        CoordinatedTurnPlan.model_validate(
            {
                "player_intent": "Вернуться в таверну",
                "resolution": "sequence",
                "action_sequence": {
                    "steps": [
                        {
                            "action_type": "movement",
                            "intent": "Вернуться в таверну",
                            "resolution": "auto_success",
                            "safe_mundane": True,
                            "observable_outcome": "Герой приходит в таверну",
                            "transition": {"required": False},
                        }
                    ]
                },
            }
        )


def test_semantic_reviewer_not_verb_lists_owns_movement_and_contact_meaning():
    prompt = TurnAuthorityPlanner.SEMANTIC_REVIEW_PROMPT

    assert "MOVEMENT/TIME" in prompt
    assert "CONTACT/IDENTITY" in prompt
    assert "Do not use keyword lists" in prompt
    assert "previously unknown physical responder must be typed" in prompt


def test_contract_issues_no_longer_reinterprets_player_text():
    plan = CoordinatedTurnPlan(player_intent="Контакт.", resolution="conversation")

    assert TurnAuthorityPlanner.contract_issues(plan, "Поговорить с трактирщиком о магах") == []
