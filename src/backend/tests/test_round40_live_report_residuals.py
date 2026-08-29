from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.models.narration_validation import NarrationValidationResult
from app.services.actor_turn_authority_guard import (
    build_actor_segment_proposals,
    segment_actor_response,
)
from app.services.prompt_policy import CURRENT_PROMPT_POLICY
from app.services.turn_authority_planner import CoordinatedTurnPlan, TurnAuthorityPlanner


def test_validator_schema_has_first_class_meta_and_speaker_failures():
    for violation_type in ("meta_language", "speaker_consistency"):
        result = NarrationValidationResult.model_validate(
            {
                "verdict": "repair_required",
                "summary": "live report regression",
                "violations": [
                    {
                        "violation_type": violation_type,
                        "severity": "error",
                        "evidence": "точный фрагмент",
                        "correction": "исправить только этот фрагмент",
                    }
                ],
            }
        )
        assert result.violations[0].violation_type == violation_type


def test_auto_success_cannot_leave_authority_without_renderable_outcome():
    with pytest.raises(ValidationError, match="observable_outcome"):
        CoordinatedTurnPlan.model_validate(
            {
                "player_intent": "Осмотреть ящик К-7 ближе",
                "resolution": "sequence",
                "action_sequence": {
                    "steps": [
                        {
                            "action_type": "observation",
                            "intent": "Осмотреть ящик К-7",
                            "resolution": "auto_success",
                            "safe_mundane": True,
                            "observable_outcome": None,
                            "transition": {"required": False},
                        }
                    ]
                },
            }
        )


def test_transition_can_be_the_renderable_outcome_for_movement():
    plan = CoordinatedTurnPlan.model_validate(
        {
            "player_intent": "Иду на Старую Марину",
            "resolution": "sequence",
            "action_sequence": {
                "steps": [
                    {
                        "action_type": "movement",
                        "intent": "Дойти до Старой Марины",
                        "resolution": "auto_success",
                        "safe_mundane": True,
                        "observable_outcome": None,
                        "transition": {
                            "required": True,
                            "transition_type": "location_transition",
                            "destination_location": "Старая Марина",
                        },
                    }
                ]
            },
        }
    )
    assert plan.action_sequence.steps[0].transition.destination_location == "Старая Марина"


def test_nested_quote_and_enclosing_sentence_create_one_actor_belief():
    text = "Докер кивает. «Это мой груз», — говорит он низким голосом."
    segments = segment_actor_response(text)
    quoted_id = next(i for i, value in enumerate(segments, start=1) if value == "Это мой груз")
    enclosing_id = next(
        i
        for i, value in enumerate(segments, start=1)
        if "Это мой груз" in value and value != "Это мой груз"
    )

    proposals = build_actor_segment_proposals(
        segments,
        [quoted_id, enclosing_id],
        acting_character_id=uuid4(),
        player_character_id=uuid4(),
    )

    assert len(proposals) == 1
    assert proposals[0].payload["proposition"] == "Это мой груз"


def test_actor_surface_contract_locks_selected_speaker_and_forbids_meta_prose():
    contract = CURRENT_PROMPT_POLICY.player_control_contract
    surface = CURRENT_PROMPT_POLICY.narrator_surface_contract

    assert "response actor" in contract
    assert "do not recycle or reassign another NPC's earlier line" in contract
    assert "internal action produced no external state change" in surface


def test_semantic_plan_reviewer_rejects_empty_success_instead_of_demanding_drama():
    review = TurnAuthorityPlanner.SEMANTIC_REVIEW_PROMPT

    assert "RENDERABLE OUTCOME" in review
    assert "empty success" in review
    assert "Do not demand drama" in review
