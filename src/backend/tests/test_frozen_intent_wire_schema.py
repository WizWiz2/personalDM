from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.services.dead_turn_guard import _empty_plan_diagnostic
from app.services.player_intent_interpreter import (
    PlayerActionIntentDraft,
    PlayerIntentContractDraft,
    _IntentWire,
    _destination_binding_wire,
    normalize_intent_draft,
)
from app.services.turn_authority_planner import CoordinatedTurnPlan
from app.services.turn_outcome_resolver import (
    OutcomeNpcIntroductionDraft,
    TurnOutcomeDecisionDraft,
    _outcome_wire_model,
    _profile_wire_model,
)


def test_movement_reference_schema_restricts_known_identity_and_allows_discovery():
    location_id = "00000000-0000-4000-8000-000000000001"
    wire = _destination_binding_wire([0], {location_id: "Комната Кая"})
    payload = {"action_0": location_id}
    assert wire.model_validate(payload).action_0 == location_id
    payload["action_0"] = "new"
    assert wire.model_validate(payload).action_0 == "new"
    payload["action_0"] = "invented-id"
    with pytest.raises(ValidationError):
        wire.model_validate(payload)


def test_intent_wire_schema_cannot_accept_empty_json_object() -> None:
    schema = PlayerIntentContractDraft.model_json_schema()

    assert {"summary", "actions"}.issubset(set(schema.get("required") or []))
    with pytest.raises(ValidationError):
        PlayerIntentContractDraft.model_validate({})


def test_destination_bindings_cannot_select_another_actions_candidate():
    refs = {"room": "Комната", "garden": "Сад"}
    wire = _destination_binding_wire([0, 1], refs, {0: {"room": "Комната"}, 1: {"garden": "Сад"}})
    with pytest.raises(ValidationError):
        wire.model_validate({"action_0": "garden", "action_1": "garden"})
    with pytest.raises(ValidationError):
        wire.model_validate({"action_0": "room", "action_1": "garden", "actions": []})


def test_intent_action_wire_schema_requires_type_and_intent() -> None:
    schema = PlayerActionIntentDraft.model_json_schema()

    assert {"action_type", "intent"}.issubset(set(schema.get("required") or []))
    with pytest.raises(ValidationError):
        PlayerActionIntentDraft.model_validate({})


@pytest.mark.parametrize("action_type", ["move", "give", "teleport"])
def test_unknown_wire_action_cannot_silently_lose_execution_authority(action_type: str) -> None:
    # These drafts previously became `other`, discarding destination/item identity while reporting
    # success. The actual model decoder must see the same finite vocabulary as the executor.
    schema = PlayerActionIntentDraft.model_json_schema()
    assert action_type not in schema["properties"]["action_type"]["enum"]
    with pytest.raises(ValidationError):
        PlayerActionIntentDraft.model_validate(
            {
                "action_type": action_type,
                "intent": "Выхожу в коридор.",
                "destination_location": "Коридор",
            }
        )


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


@pytest.mark.parametrize(
    "action",
    [
        {"action_type": "movement", "intent": "Выйти в коридор."},
        {"action_type": "inventory", "intent": "Вернуть ключ.", "inventory_operation": "give"},
        {
            "action_type": "inventory",
            "intent": "Вернуть ключ.",
            "inventory_operation": "give",
            "item_id": "00000000-0000-4000-8000-000000000001",
        },
        {"action_type": "interaction", "intent": "Вернуть ключ.", "inventory_operation": "give"},
    ],
)
def test_wire_requires_executable_fields_for_each_action_kind(action: dict) -> None:
    with pytest.raises(ValidationError):
        _IntentWire.model_validate({"summary": "Действие игрока.", "actions": [action]})


def test_wire_normalization_preserves_movement_and_inventory_authority() -> None:
    wire = _IntentWire.model_validate(
        {
            "summary": "Кай выходит в коридор и передаёт ключ Мартину.",
            "actions": [
                {
                    "action_type": "movement",
                    "actor_role": "speaker",
                    "intent": "Выхожу в коридор.",
                    "destination_location": "Коридор",
                    "movement_method": "ordinary",
                },
                {
                    "action_type": "inventory",
                    "actor_role": "speaker",
                    "intent": "Передаю ключ Мартину.",
                    "inventory_operation": "give",
                    "item_id": "00000000-0000-4000-8000-000000000001",
                    "inventory_target_id": "00000000-0000-4000-8000-000000000002",
                },
            ],
        }
    )
    draft = PlayerIntentContractDraft.model_validate(wire.model_dump(mode="json"))
    contract = normalize_intent_draft(draft, wire.summary)
    assert [action.action_type for action in contract.actions] == ["movement", "inventory"]
    assert contract.actions[0].destination_location == "Коридор"
    assert str(contract.actions[1].item_id) == "00000000-0000-4000-8000-000000000001"
    assert str(contract.actions[1].inventory_target_id) == "00000000-0000-4000-8000-000000000002"
    assert contract.actions[1].inventory_operation == "give"


def test_special_movement_method_survives_intent_normalization():
    wire = _IntentWire.model_validate(
        {
            "summary": "Телепортируюсь в коридор.",
            "actions": [
                {
                    "action_type": "movement",
                    "actor_role": "speaker",
                    "intent": "Телепортироваться в коридор.",
                    "destination_location": "Коридор",
                    "movement_method": "teleportation",
                }
            ],
        }
    )
    draft = PlayerIntentContractDraft.model_validate(wire.model_dump(mode="json"))
    assert normalize_intent_draft(draft, wire.summary).actions[0].movement_method == "special"


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
            "npc_introductions": [],
            "action_outcomes": [
                {"action_index": 0, "resolution": "auto_success"},
                {"action_index": 1, "resolution": "auto_success"},
            ],
        }
    )
    assert [item.action_index for item in parsed.action_outcomes] == [0, 1]


def test_actionless_turn_requires_a_response_without_inventing_an_action() -> None:
    wire = _outcome_wire_model(0)
    with pytest.raises(ValidationError):
        wire.model_validate({"action_outcomes": []})
    parsed = wire.model_validate(
        {
            "action_outcomes": [],
            "npc_introductions": [],
            "observable_consequences": ["Мартин отвечает кивком."],
        }
    )
    assert parsed.action_outcomes == []


def test_npc_wire_cannot_omit_encounter_evidence_and_profile() -> None:
    with pytest.raises(ValidationError):
        OutcomeNpcIntroductionDraft.model_validate({"canonical_name": "Мартин Вэнс"})


def test_resolved_player_choice_cannot_be_reopened_by_outcome() -> None:
    wire = _outcome_wire_model(1, allow_choice=False)
    with pytest.raises(ValidationError):
        wire.model_validate(
            {
                "npc_introductions": [],
                "action_outcomes": [{"action_index": 0, "resolution": "requires_choice"}],
            }
        )


@pytest.mark.parametrize("resolution", ["auto_success", "blocked"])
def test_outcome_requires_result_or_blocker(resolution) -> None:
    with pytest.raises(ValidationError):
        _outcome_wire_model(1, allow_choice=False).model_validate(
            {
                "npc_introductions": [],
                "action_outcomes": [{"action_index": 0, "resolution": resolution}],
            }
        )


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
