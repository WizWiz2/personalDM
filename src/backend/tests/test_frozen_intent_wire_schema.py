from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.services.dead_turn_guard import _empty_plan_diagnostic
from app.services.player_intent_interpreter import (
    PlayerActionIntentDraft,
    PlayerIntentContractDraft,
    normalize_intent_draft,
)
from app.services.turn_authority_planner import CoordinatedTurnPlan
from app.services.turn_outcome_resolver import (
    TurnOutcomeDecisionDraft,
    _outcome_wire_model,
    _profile_wire_model,
)


def test_intent_wire_schema_cannot_accept_empty_json_object() -> None:
    schema = PlayerIntentContractDraft.model_json_schema()

    assert {"summary", "actions"}.issubset(set(schema.get("required") or []))
    with pytest.raises(ValidationError):
        PlayerIntentContractDraft.model_validate({})


def test_intent_action_wire_schema_requires_type_and_intent() -> None:
    schema = PlayerActionIntentDraft.model_json_schema()

    assert {"action_type", "intent"}.issubset(set(schema.get("required") or []))
    with pytest.raises(ValidationError):
        PlayerActionIntentDraft.model_validate({})


def test_required_movement_draft_still_normalizes_into_strict_ir() -> None:
    draft = PlayerIntentContractDraft.model_validate(
        {
            "summary": "Кай выходит из комнаты в коридор.",
            "actions": [
                {
                    "action_type": "movement",
                    "intent": "Выйти из комнаты в коридор.",
                    "destination_location": "Коридор",
                }
            ],
        }
    )

    contract = normalize_intent_draft(draft, "Я выхожу из своей комнаты в коридор.")

    assert len(contract.actions) == 1
    assert contract.actions[0].action_type == "movement"
    assert contract.actions[0].destination_location == "Коридор"


def test_outcome_wire_schema_requires_action_outcomes_field() -> None:
    schema = TurnOutcomeDecisionDraft.model_json_schema()

    assert "action_outcomes" in set(schema.get("required") or [])
    with pytest.raises(ValidationError):
        TurnOutcomeDecisionDraft.model_validate({})


def test_outcome_wire_schema_enforces_frozen_action_cardinality() -> None:
    response_model = _outcome_wire_model(2)

    with pytest.raises(ValidationError):
        response_model.model_validate({"action_outcomes": []})
    with pytest.raises(ValidationError):
        response_model.model_validate(
            {
                "action_outcomes": [
                    {"action_index": 0, "resolution": "auto_success"},
                ]
            }
        )

    parsed = response_model.model_validate(
        {
            "action_outcomes": [
                {"action_index": 0, "resolution": "auto_success"},
                {"action_index": 1, "resolution": "auto_success"},
            ]
        }
    )
    assert [item.action_index for item in parsed.action_outcomes] == [0, 1]


def test_profile_wire_schema_enforces_requested_patch_cardinality() -> None:
    response_model = _profile_wire_model(1)

    with pytest.raises(ValidationError):
        response_model.model_validate({"patches": []})

    parsed = response_model.model_validate(
        {
            "patches": [
                {
                    "action_index": 0,
                    "profile": (
                        "Круглосуточная прачечная занимает светлое помещение первого этажа; "
                        "вдоль стен стоят ряды машин, столы для белья и обычная зона ожидания."
                    ),
                }
            ]
        }
    )
    assert len(parsed.patches) == 1


def test_empty_plan_diagnostic_exposes_actual_shape() -> None:
    plan = CoordinatedTurnPlan.conservative_fallback("Я выхожу в коридор.")

    diagnostic = _empty_plan_diagnostic(plan)

    assert "action_steps=0" in diagnostic
    assert "transition_required=False" in diagnostic
    assert "observable_consequences=0" in diagnostic
