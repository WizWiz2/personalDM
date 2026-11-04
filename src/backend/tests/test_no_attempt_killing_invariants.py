"""Round 2: no code-side check kills an attempt the model could not have avoided. A rule is either a
schema constraint the model cannot violate, or it is not one of the four bans and is dropped."""
from uuid import uuid4

from app.models.turn import ChatMessage
from app.services.player_intent_interpreter import _intent_wire_model, stated_act_wire
from app.services.turn_outcome_resolver import _outcome_wire_model


def test_every_stated_act_has_an_existing_entry_by_schema():
    wire = stated_act_wire(_intent_wire_model([]), ("вынимаю", "вкладываю"))
    schema = wire.model_json_schema()
    assert schema["properties"]["actions"]["minItems"] == 2
    entries = schema["$defs"]["StatedActEntries"]["properties"]
    assert {entry["maximum"] for entry in entries.values()} == {1}


def test_a_property_effect_without_its_subject_is_not_a_representable_local_action():
    item = str(uuid4())
    context = [ChatMessage(role="system", content=(
        f"Player-owned items: Кошель [id={item}]\nPhysically present characters: Тарин [id={uuid4()}]"))]
    schema = _intent_wire_model(context).model_json_schema()
    for name, model in schema["$defs"].items():
        effects = set(model.get("properties", {}).get("expected_effect", {}).get("enum", []))
        if effects & {"item_change", "item_read"} and "contribution_kind" in model["properties"] \
                and model["properties"]["contribution_kind"].get("const") != "speech" \
                and model["properties"]["contribution_kind"].get("enum") != ["speech"]:
            subject = model["properties"]["subject_item_id"]
            assert "anyOf" not in subject and "subject_item_id" in model["required"], name


def test_success_without_a_typed_effect_or_errand_target_is_not_an_attempt_killer():
    wire = _outcome_wire_model(2, required_effects={0: "item_change", 1: "fetch_person"})
    parsed = wire.model_validate({"npc_introductions": [], "action_outcomes": [
        {"action_index": 0, "resolution": "auto_success"},
        {"action_index": 1, "resolution": "auto_success", "npc_response": "accepted"}]})
    assert len(parsed.action_outcomes) == 2


def test_a_known_addressee_elsewhere_gets_a_typed_silence_not_an_enum_failure():
    wire = _outcome_wire_model(0, present_names=["Борм", "Тарин"], addressee_elsewhere=True)
    assert wire.model_json_schema()["properties"]["response_speaker_name"].get("type") == "null"
