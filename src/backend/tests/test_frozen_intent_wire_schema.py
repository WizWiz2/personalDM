from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.turn import ChatMessage
from app.services.dead_turn_guard import _empty_plan_diagnostic
from app.services.player_intent_interpreter import (
    PlayerActionIntentDraft,
    PlayerIntentContractDraft,
    _destination_binding_wire,
    _intent_semantic_review_wire,
    _intent_wire_model,
    _IntentWire,
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


def test_inventory_schema_rejects_a_one_digit_uuid_error_and_self_recipient():
    item = "00000000-0000-4000-8000-000000000001"
    player = "00000000-0000-4000-8000-000000000002"
    npc = "00000000-0000-4000-8000-000000000003"
    wire = _intent_wire_model([ChatMessage(role="system", content=(
        f"Controlled character: {player}\nPlayer-owned items: Ключ [id={item}]\n"
        f"Physically present characters: Кай [id={player}], Мартин [id={npc}]"
    ))])
    action = dict(action_type="inventory", actor_role="speaker", intent="Передаю ключ.",
                  item_id=item, inventory_operation="give", inventory_target_id=npc)
    payload = {"summary": "Передаю ключ.", "actions": [action]}
    assert wire.model_validate(payload).actions[0].item_id == item
    for field, bad_id in (("item_id", item[:-1] + "9"), ("inventory_target_id", player)):
        with pytest.raises(ValidationError):
            wire.model_validate({**payload, "actions": [{**action, field: bad_id}]})
    with pytest.raises(ValidationError, match="own addressee"):
        wire.model_validate({"summary": "Спрашиваю дежурного.", "actions": [],
                             "addressed_character_name": "Кай"})


def test_empty_action_schema_exposes_only_null_prerequisites():
    schema = _outcome_wire_model(0).model_json_schema()
    assert schema["properties"]["response_after_action_index"]["type"] == "null"
    intro = schema["$defs"]["IndexedNpcIntroductionDraft"]
    assert intro["properties"]["after_action_index"]["type"] == "null"


def test_attempted_movement_cannot_be_combined_with_no_endpoint_or_travel_effect():
    wire = _intent_semantic_review_wire(1, "Пытаюсь пройти на склад; прямого прохода нет.")
    action = {"action_index": 0, "actor_role": "speaker", "contribution_kind": "world_action",
              "action_type": "movement", "spatial_effect": "none", "destination_location": None}
    payload = {"action_ownership": [action], "information_request_only": False,
               "information_recipient": "none"}
    with pytest.raises(ValidationError, match="attempted movement"):
        wire.model_validate(payload)
    valid = {**action, "spatial_effect": "travel", "destination_location": "Склад"}
    assert wire.model_validate({**payload, "action_ownership": [valid]}).action_ownership[0].action_type == "movement"
    schema = wire.model_json_schema()
    movement = schema["$defs"]["MovingOwnership"]["properties"]
    assert movement["spatial_effect"]["const"] == "travel"
    assert movement["destination_location"]["type"] == "string"


def test_intent_wire_schema_cannot_accept_empty_json_object() -> None:
    schema = PlayerIntentContractDraft.model_json_schema()

    assert {"summary", "actions"}.issubset(set(schema.get("required") or []))
    with pytest.raises(ValidationError):
        PlayerIntentContractDraft.model_validate({})


def test_destination_bindings_select_only_catalogued_ids_or_new():
    wire = _destination_binding_wire([0, 1], {"room": "Комната", "garden": "Сад"})
    assert wire.model_validate({"action_0": "garden", "action_1": "new"})
    with pytest.raises(ValidationError):
        wire.model_validate({"action_0": "Сад", "action_1": "garden"})
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
    assert contract.actions[0].destination_within_origin is False
    draft.actions[0].destination_reference = "new_inside"
    inside = normalize_intent_draft(draft, "Я выхожу из своей комнаты в коридор.").actions[0]
    assert (inside.destination_location_id, inside.destination_within_origin) == (None, True)


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


def test_unfilled_resident_slot_is_introduced_through_npc_introductions() -> None:
    wire = _outcome_wire_model(0, resident_slots=["inn", "town"], required_slot="inn")
    base = {"action_outcomes": [], "observable_consequences": ["За стойкой стоит хозяин."]}
    keeper = {
        "canonical_name": "Трактирщик", "role": "трактирщик", "reason": "Хозяин за стойкой.",
        "description": "Плотный немолодой мужчина в фартуке, хозяин этого трактира.",
        "appearance": "Седая борода, закатанные рукава, полотенце через плечо.",
        "resident_slot": "inn",
    }
    with pytest.raises(ValidationError):
        wire.model_validate({**base, "npc_introductions": []})
    with pytest.raises(ValidationError):
        wire.model_validate({**base, "npc_introductions": [{**keeper, "resident_slot": "town"}]})
    parsed = wire.model_validate({**base, "npc_introductions": [keeper]})
    assert parsed.npc_introductions[0].resident_slot == "inn"


def test_addressee_can_summon_a_newcomer_only_through_a_frozen_action() -> None:
    wire = _outcome_wire_model(1, bound_response_speaker="Хозяин")
    servant = {
        "canonical_name": "Половой", "role": "половой", "reason": "Хозяин позвал его из подсобки.",
        "description": "Молодой парень в косоворотке, ведёт книгу постояльцев.",
        "appearance": "Вихрастый, с карандашом за ухом и полотенцем на плече.",
    }
    base = {
        "action_outcomes": [{"action_index": 0, "resolution": "auto_success"}],
        "response_speaker_name": "Хозяин",
    }
    with pytest.raises(ValidationError):
        wire.model_validate({**base, "npc_introductions": [servant]})
    parsed = wire.model_validate({**base, "npc_introductions": [{**servant, "after_action_index": 0}]})
    assert parsed.npc_introductions[0].after_action_index == 0


def _revealed_response(*, evidence: str):
    return {
        "action_outcomes": [], "npc_introductions": [],
        "observable_consequences": ["Дежурный отрывается от журнала."],
        "response_revealed_name": "Мартин", "response_name_evidence": evidence,
    }


def test_typed_name_change_keeps_its_self_identification_evidence():
    parsed = _outcome_wire_model(0).model_validate(_revealed_response(evidence="Меня зовут Мартин."))
    assert parsed.response_revealed_name == "Мартин"
    assert parsed.response_name_evidence == "Меня зовут Мартин."


def test_name_change_without_matching_self_identification_is_rejected():
    with pytest.raises(ValidationError, match="self-identification"):
        _outcome_wire_model(0).model_validate(_revealed_response(evidence="Меня зовут Эдгар."))


def test_revealed_name_cannot_replace_the_existing_response_owner():
    wire = _outcome_wire_model(0, bound_response_speaker="Дежурный у стойки")
    payload = _revealed_response(evidence="Я Мартин.")
    payload["response_speaker_name"] = "Мартин"
    with pytest.raises(ValidationError):
        wire.model_validate(payload)
    payload["response_speaker_name"] = "Дежурный у стойки"
    parsed = wire.model_validate(payload)
    assert parsed.response_speaker_name == "Дежурный у стойки"
    assert parsed.response_revealed_name == "Мартин"
    schema = wire.model_json_schema()["properties"]["response_speaker_name"]
    assert schema["anyOf"][0]["const"] == "Дежурный у стойки"


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
                    "name": "Прачечная",
                    "within_current": False,
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
