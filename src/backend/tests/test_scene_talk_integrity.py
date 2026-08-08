import pytest
from pydantic import ValidationError

from app.models.narration_validation import NarrationValidationResult
from app.services.narration_validator import NarrationValidator
from app.services.turn_planner import TurnPlan


def _base_plan(**updates):
    data = {
        "player_intent": "Игрок идёт в таверну.",
        "resolution": "observation",
        "observable_consequences": [],
        "character_beats": [],
        "canon_constraints": [],
        "new_fact_candidates": [],
        "narration_guidance": [],
        "ending_hook": "",
    }
    data.update(updates)
    return data


def test_transition_resolution_requires_structured_boundary():
    with pytest.raises(ValidationError):
        TurnPlan.model_validate(_base_plan(resolution="transition"))


def test_transition_resolution_accepts_materializable_location():
    plan = TurnPlan.model_validate(
        _base_plan(
            resolution="transition",
            scene_transition={
                "required": True,
                "transition_type": "location_transition",
                "destination_location": "Гнилой фонарь",
            },
        )
    )
    assert plan.scene_transition.required is True
    assert plan.scene_transition.destination_location == "Гнилой фонарь"


def test_confirmed_talk_speaker_cannot_be_rejected_as_absent():
    result = NarrationValidationResult.model_validate(
        {
            "verdict": "repair_required",
            "summary": "Скорняк отсутствует.",
            "violations": [
                {
                    "violation_type": "absent_character",
                    "severity": "error",
                    "evidence": "Скорняк отвечает игроку, хотя его нет в сцене.",
                    "correction": "Уберите реплику Скорняка.",
                }
            ],
        }
    )

    protected = NarrationValidator.protect_confirmed_speaker(result, "Скорняк")

    assert protected.verdict == "pass"
    assert protected.violations == []


def test_confirmed_speaker_does_not_hide_other_absent_characters():
    result = NarrationValidationResult.model_validate(
        {
            "verdict": "repair_required",
            "summary": "В сцену попал отсутствующий персонаж.",
            "violations": [
                {
                    "violation_type": "absent_character",
                    "severity": "error",
                    "evidence": "Кассиан внезапно входит в лавку.",
                    "correction": "Уберите появление Кассиана.",
                }
            ],
        }
    )

    protected = NarrationValidator.protect_confirmed_speaker(result, "Скорняк")

    assert protected.verdict == "repair_required"
    assert len(protected.violations) == 1
    assert "Кассиан" in protected.violations[0].evidence
